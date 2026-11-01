"""FastAPI application factory and routes for the local Job Hunter dashboard."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated

import jinja2
from fastapi import Depends, FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, sessionmaker

from job_hunter import DEFAULT_CONFIG_DIR, PROJECT_ROOT
from job_hunter.config import AppConfig, ConfigError, Profile, load_config, load_search, load_skills
from job_hunter.core.db import get_engine, init_db, make_session_factory
from job_hunter.core.models import STATUSES, Job
from job_hunter.core.skills import SkillMatcher
from job_hunter.dashboard.queries import (
    InvalidInputError,
    JobNotFoundError,
    explain_job,
    export_jobs_csv,
    get_cv_options,
    get_job_events,
    get_platforms,
    get_recent_runs,
    get_status_counts,
    parse_filter_params,
    query_jobs_page,
    set_job_cv,
    set_job_notes,
    set_job_status,
)
from job_hunter.dashboard.security import (
    LocalhostSecurityMiddleware,
    safe_external_url,
    safe_local_redirect,
)

DASHBOARD_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = DASHBOARD_DIR / "templates"
STATIC_DIR = DASHBOARD_DIR / "static"

_STATUS_SLUGS = {st: st.lower().replace(" ", "-") for st in STATUSES}


def status_slug(status: str | None) -> str:
    return _STATUS_SLUGS.get(status or "", "new")


def score_tier(score: int | None, threshold: int = 50) -> str:
    if score is None:
        return "low"
    if score >= threshold:
        return "high"
    if score >= max(30, threshold - 15):
        return "mid"
    return "low"


def fmt_dt(dt: datetime | None, *, include_time: bool = True) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M") if include_time else dt.strftime("%Y-%m-%d")


def _build_templates() -> Jinja2Templates:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
    )
    assert env.autoescape is True  # Hard security invariant: never render unescaped scraped data
    env.globals.update(
        STATUSES=STATUSES,
        status_slug=status_slug,
        score_tier=score_tier,
        safe_external_url=safe_external_url,
        fmt_dt=fmt_dt,
    )
    return Jinja2Templates(env=env)


def _resolve_config(
    cfg: AppConfig | None,
    config_dir: Path | None,
    root: Path | None,
) -> AppConfig:
    if cfg is not None:
        return cfg
    resolved_dir = Path(config_dir or DEFAULT_CONFIG_DIR)
    resolved_root = Path(root or PROJECT_ROOT)
    try:
        return load_config(resolved_dir, resolved_root)
    except ConfigError:
        # Fallback when gitignored profile.yaml / cvs.yaml are absent (e.g. standalone test DB).
        if config_dir is not None:
            raise
        search = load_search(DEFAULT_CONFIG_DIR / "search.yaml")
        skills = load_skills(DEFAULT_CONFIG_DIR / "skills.yaml")
        return AppConfig(
            root=resolved_root,
            config_dir=DEFAULT_CONFIG_DIR,
            profile=Profile(first_name="", last_name="", email="", core_skills=[]),
            cvs=[],
            search=search,
            skills=skills,
            warnings=[],
        )


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request", "").strip().lower() == "true"


def _get_session(request: Request) -> Iterator[Session]:
    factory: sessionmaker[Session] = request.app.state.session_factory
    s = factory()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


SessionDep = Annotated[Session, Depends(_get_session)]


def create_app(
    *,
    db_path: str | Path | None = None,
    session_factory: sessionmaker[Session] | None = None,
    cfg: AppConfig | None = None,
    config_dir: Path | None = None,
    root: Path | None = None,
) -> FastAPI:
    """Create the local-only FastAPI dashboard application."""
    resolved_cfg = _resolve_config(cfg, config_dir, root)
    matcher = SkillMatcher(resolved_cfg.skills)
    templates = _build_templates()

    owned_engine = None
    if session_factory is None:
        owned_engine = get_engine(db_path)
        init_db(owned_engine)
        factory = make_session_factory(owned_engine)
    else:
        factory = session_factory

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if owned_engine is not None:
                owned_engine.dispose()

    app = FastAPI(
        title="Job Hunter Dashboard",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(LocalhostSecurityMiddleware)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.state.templates = templates
    app.state.cfg = resolved_cfg
    app.state.matcher = matcher
    app.state.session_factory = factory

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        session: SessionDep,
        status: str | None = Query(default=None),
        platform: str | None = Query(default=None),
        min_score: str | None = Query(default=None),
        max_score: str | None = Query(default=None),
        date_from: str | None = Query(default=None),
        date_to: str | None = Query(default=None),
        q: str | None = Query(default=None),
        sort: str | None = Query(default=None),
        order: str | None = Query(default=None),
        page: str | None = Query(default=None),
        per_page: str | None = Query(default=None),
    ) -> Response:
        params = parse_filter_params(
            status=status,
            platform=platform,
            min_score=min_score,
            max_score=max_score,
            date_from=date_from,
            date_to=date_to,
            q=q,
            sort=sort,
            order=order,
            page=page,
            per_page=per_page,
        )
        jobs, total_matching, total_pages = query_jobs_page(session, params)
        status_counts, total_all = get_status_counts(session)
        platforms = get_platforms(session)
        runs = get_recent_runs(session)
        cv_options = get_cv_options(session, resolved_cfg)
        effective_page = min(params.page, total_pages)
        current_qs = params.query_string(include_sort=True, include_pagination=True)
        current_path = f"/?{current_qs}" if current_qs else "/"

        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "params": params,
                "jobs": jobs,
                "total_matching": total_matching,
                "total_pages": total_pages,
                "effective_page": effective_page,
                "status_counts": status_counts,
                "total_all": total_all,
                "platforms": platforms,
                "runs": runs,
                "cv_options": cv_options,
                "threshold": resolved_cfg.search.shortlist_threshold,
                "current_path": current_path,
                "export_qs": params.query_string(include_sort=True, include_pagination=False),
            },
        )

    @app.get("/partials/counters", response_class=HTMLResponse)
    def counters_partial(
        request: Request,
        session: SessionDep,
        status: str | None = Query(default=None),
    ) -> Response:
        params = parse_filter_params(status=status)
        status_counts, total_all = get_status_counts(session)
        return templates.TemplateResponse(
            request,
            "_status_counters.html",
            {
                "params": params,
                "status_counts": status_counts,
                "total_all": total_all,
            },
        )

    @app.get("/jobs/export.csv")
    def export_csv(
        session: SessionDep,
        status: str | None = Query(default=None),
        platform: str | None = Query(default=None),
        min_score: str | None = Query(default=None),
        max_score: str | None = Query(default=None),
        date_from: str | None = Query(default=None),
        date_to: str | None = Query(default=None),
        q: str | None = Query(default=None),
        sort: str | None = Query(default=None),
        order: str | None = Query(default=None),
    ) -> Response:
        params = parse_filter_params(
            status=status,
            platform=platform,
            min_score=min_score,
            max_score=max_score,
            date_from=date_from,
            date_to=date_to,
            q=q,
            sort=sort,
            order=order,
        )
        csv_data = export_jobs_csv(session, params)
        return Response(
            content=csv_data,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="jobs_export.csv"'},
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail(
        request: Request,
        job_id: int,
        session: SessionDep,
    ) -> Response:
        job = session.get(Job, job_id)
        if job is None:
            return PlainTextResponse(f"Job {job_id} not found", status_code=404)
        evaluation = explain_job(session, job, resolved_cfg, matcher)
        cv_options = get_cv_options(session, resolved_cfg)
        events = get_job_events(session, job.id)
        return templates.TemplateResponse(
            request,
            "job_detail.html",
            {
                "job": job,
                "ev": evaluation,
                "cv_options": cv_options,
                "events": events,
                "threshold": resolved_cfg.search.shortlist_threshold,
                "saved": False,
            },
        )

    def _mutation_response(
        request: Request,
        session: Session,
        job: Job,
        *,
        context: str,
        next_url: str,
        trigger_counters: bool = False,
    ) -> Response:
        if _is_htmx(request):
            cv_options = get_cv_options(session, resolved_cfg)
            headers = {"HX-Trigger": "jobsChanged"} if trigger_counters else None
            if context == "detail":
                evaluation = explain_job(session, job, resolved_cfg, matcher)
                events = get_job_events(session, job.id)
                return templates.TemplateResponse(
                    request,
                    "_detail_body.html",
                    {
                        "job": job,
                        "ev": evaluation,
                        "cv_options": cv_options,
                        "events": events,
                        "threshold": resolved_cfg.search.shortlist_threshold,
                        "saved": True,
                    },
                    headers=headers,
                )
            return templates.TemplateResponse(
                request,
                "_job_row.html",
                {
                    "job": job,
                    "cv_options": cv_options,
                    "threshold": resolved_cfg.search.shortlist_threshold,
                    "current_path": safe_local_redirect(next_url, fallback="/"),
                },
                headers=headers,
            )

        target = safe_local_redirect(next_url, fallback=f"/jobs/{job.id}")
        return RedirectResponse(url=target, status_code=303)

    @app.post("/jobs/{job_id}/status")
    def update_status(
        request: Request,
        job_id: int,
        session: SessionDep,
        status: str = Form(default=""),
        context: str = Form(default="row"),
        next_url: str = Form(default=""),
    ) -> Response:
        try:
            job = set_job_status(session, job_id, status)
        except JobNotFoundError as exc:
            return PlainTextResponse(str(exc), status_code=404)
        except InvalidInputError as exc:
            return PlainTextResponse(str(exc), status_code=422)
        session.flush()
        return _mutation_response(
            request,
            session,
            job,
            context=context,
            next_url=next_url,
            trigger_counters=True,
        )

    @app.post("/jobs/{job_id}/cv")
    def update_cv(
        request: Request,
        job_id: int,
        session: SessionDep,
        cv_id: str = Form(default=""),
        context: str = Form(default="row"),
        next_url: str = Form(default=""),
    ) -> Response:
        try:
            job = set_job_cv(session, job_id, cv_id)
        except JobNotFoundError as exc:
            return PlainTextResponse(str(exc), status_code=404)
        except InvalidInputError as exc:
            return PlainTextResponse(str(exc), status_code=422)
        session.flush()
        return _mutation_response(request, session, job, context=context, next_url=next_url)

    @app.post("/jobs/{job_id}/notes")
    def update_notes(
        request: Request,
        job_id: int,
        session: SessionDep,
        notes: str = Form(default=""),
        context: str = Form(default="row"),
        next_url: str = Form(default=""),
    ) -> Response:
        try:
            job = set_job_notes(session, job_id, notes)
        except JobNotFoundError as exc:
            return PlainTextResponse(str(exc), status_code=404)
        session.flush()
        return _mutation_response(request, session, job, context=context, next_url=next_url)

    return app
