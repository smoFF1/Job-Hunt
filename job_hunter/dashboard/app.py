"""FastAPI application factory and routes for the local Job Hunter dashboard."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import jinja2
from fastapi import Depends, FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, sessionmaker

import job_hunter.scrapers  # noqa: F401 - ensure scraper registry is populated
from job_hunter import DEFAULT_CONFIG_DIR, PROJECT_ROOT
from job_hunter.config import AppConfig, ConfigError, Profile, load_config, load_search, load_skills
from job_hunter.core.db import get_engine, init_db, make_session_factory, resolve_db_path
from job_hunter.core.models import STATUSES, Job
from job_hunter.core.skills import SkillMatcher
from job_hunter.dashboard.queries import (
    InvalidInputError,
    JobNotFoundError,
    explain_job,
    export_jobs_csv,
    get_cv_options,
    get_job_events,
    get_new_jobs_count,
    get_new_since_cutoff,
    get_platforms,
    get_recent_runs,
    get_status_counts,
    is_job_new,
    parse_filter_params,
    query_jobs_page,
    set_job_cv,
    set_job_notes,
    set_job_status,
)
from job_hunter.dashboard.security import (
    CsrfError,
    LocalhostSecurityMiddleware,
    safe_external_url,
    safe_local_redirect,
    verify_csrf,
)
from job_hunter.scrapers.scan import (
    ScanAlreadyRunningError,
    ScanCooldownError,
    ScanManager,
    ScanValidationError,
    cleanup_stale_scan,
    get_site_scan_statuses,
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
        is_job_new=is_job_new,
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


def _resolve_db_path_for_app(
    db_path: str | Path | None,
    session_factory: sessionmaker[Session] | None,
) -> Path:
    if db_path is not None:
        return resolve_db_path(db_path)
    if session_factory is not None:
        bind = getattr(session_factory, "kw", {}).get("bind")
        db_name = getattr(getattr(bind, "url", None), "database", None)
        if db_name and db_name != ":memory:":
            return Path(db_name).resolve()
    return resolve_db_path(None)


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


def _scan_panel_context(
    session: Session,
    scan_manager: ScanManager,
    cfg: AppConfig,
    *,
    scan_error: str | None = None,
) -> dict[str, Any]:
    scan_state = scan_manager.get_status()
    site_statuses = get_site_scan_statuses(session, cfg, scan_manager.db_path)
    any_selectable = any(st.selectable for st in site_statuses)
    return {
        "scan_state": scan_state,
        "site_statuses": site_statuses,
        "any_selectable": any_selectable,
        "scan_error": scan_error,
        "threshold": cfg.search.shortlist_threshold,
    }


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
    effective_config_dir = Path(config_dir or resolved_cfg.config_dir)
    effective_root = Path(root or resolved_cfg.root)
    matcher = SkillMatcher(resolved_cfg.skills)
    templates = _build_templates()

    resolved_db = _resolve_db_path_for_app(db_path, session_factory)

    owned_engine = None
    if session_factory is None:
        owned_engine = get_engine(resolved_db)
        init_db(owned_engine)
        factory = make_session_factory(owned_engine)
    else:
        factory = session_factory

    # Clean up any stale "running" scan state or orphaned lock left by a crashed worker.
    cleanup_stale_scan(resolved_db, factory)

    scan_manager = ScanManager(
        db_path=resolved_db,
        config_dir=effective_config_dir,
        root=effective_root,
        session_factory=factory,
        cfg=resolved_cfg,
    )
    csrf_secret = secrets.token_bytes(32)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        cleanup_stale_scan(resolved_db, factory)
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
    app.add_middleware(LocalhostSecurityMiddleware, csrf_secret=csrf_secret)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.exception_handler(CsrfError)
    async def _handle_csrf_error(_request: Request, exc: CsrfError) -> PlainTextResponse:
        return PlainTextResponse(str(exc), status_code=403)

    app.state.templates = templates
    app.state.cfg = resolved_cfg
    app.state.matcher = matcher
    app.state.session_factory = factory
    app.state.db_path = resolved_db
    app.state.config_dir = effective_config_dir
    app.state.root = effective_root
    app.state.scan_manager = scan_manager
    app.state.csrf_secret = csrf_secret

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
        new_only: str | None = Query(default=None),
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
            new_only=new_only,
            sort=sort,
            order=order,
            page=page,
            per_page=per_page,
        )
        new_since_cutoff = get_new_since_cutoff(session)
        new_jobs_count = get_new_jobs_count(session, new_since_cutoff)
        jobs, total_matching, total_pages = query_jobs_page(session, params, new_since_cutoff=new_since_cutoff)
        status_counts, total_all = get_status_counts(session)
        platforms = get_platforms(session)
        runs = get_recent_runs(session)
        cv_options = get_cv_options(session, resolved_cfg)
        effective_page = min(params.page, total_pages)
        current_qs = params.query_string(include_sort=True, include_pagination=True)
        current_path = f"/?{current_qs}" if current_qs else "/"
        scan_ctx = _scan_panel_context(session, scan_manager, resolved_cfg)

        return templates.TemplateResponse(
            request,
            "index.html",
            {
                **scan_ctx,
                "params": params,
                "jobs": jobs,
                "total_matching": total_matching,
                "total_pages": total_pages,
                "effective_page": effective_page,
                "status_counts": status_counts,
                "total_all": total_all,
                "new_since_cutoff": new_since_cutoff,
                "new_jobs_count": new_jobs_count,
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
        new_only: str | None = Query(default=None),
    ) -> Response:
        params = parse_filter_params(status=status, new_only=new_only)
        status_counts, total_all = get_status_counts(session)
        new_since_cutoff = get_new_since_cutoff(session)
        new_jobs_count = get_new_jobs_count(session, new_since_cutoff)
        return templates.TemplateResponse(
            request,
            "_status_counters.html",
            {
                "params": params,
                "status_counts": status_counts,
                "total_all": total_all,
                "new_jobs_count": new_jobs_count,
            },
        )

    @app.get("/scan/status", response_class=HTMLResponse)
    def scan_status_partial(
        request: Request,
        session: SessionDep,
    ) -> Response:
        ctx = _scan_panel_context(session, scan_manager, resolved_cfg)
        headers = (
            {"HX-Trigger": "jobsChanged"}
            if _is_htmx(request) and ctx["scan_state"].get("state") != "running"
            else None
        )
        return templates.TemplateResponse(request, "_scan_panel.html", ctx, headers=headers)

    @app.post("/scan", dependencies=[Depends(verify_csrf)])
    def trigger_scan(
        request: Request,
        session: SessionDep,
        sites: Annotated[list[str] | None, Form()] = None,
        extra_query: str = Form(default=""),
        dry_run: str = Form(default=""),
    ) -> Response:
        is_dry = dry_run.strip().lower() in ("1", "true", "on", "yes")
        try:
            scan_manager.start_scan(
                sites=sites or [],
                extra_query=extra_query,
                dry_run=is_dry,
            )
        except ScanAlreadyRunningError as exc:
            ctx = _scan_panel_context(session, scan_manager, resolved_cfg, scan_error=str(exc))
            return templates.TemplateResponse(request, "_scan_panel.html", ctx, status_code=409)
        except ScanCooldownError as exc:
            ctx = _scan_panel_context(session, scan_manager, resolved_cfg, scan_error=str(exc))
            return templates.TemplateResponse(request, "_scan_panel.html", ctx, status_code=429)
        except ScanValidationError as exc:
            ctx = _scan_panel_context(session, scan_manager, resolved_cfg, scan_error=str(exc))
            return templates.TemplateResponse(request, "_scan_panel.html", ctx, status_code=422)

        if _is_htmx(request):
            ctx = _scan_panel_context(session, scan_manager, resolved_cfg)
            return templates.TemplateResponse(request, "_scan_panel.html", ctx)
        return RedirectResponse(url="/", status_code=303)

    @app.post("/scan/stop", dependencies=[Depends(verify_csrf)])
    def stop_scan_route(
        request: Request,
        session: SessionDep,
    ) -> Response:
        scan_manager.stop_scan()
        if _is_htmx(request):
            ctx = _scan_panel_context(session, scan_manager, resolved_cfg)
            return templates.TemplateResponse(
                request,
                "_scan_panel.html",
                ctx,
                headers={"HX-Trigger": "jobsChanged"},
            )
        return RedirectResponse(url="/", status_code=303)

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
        new_only: str | None = Query(default=None),
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
            new_only=new_only,
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
        new_since_cutoff = get_new_since_cutoff(session)
        return templates.TemplateResponse(
            request,
            "job_detail.html",
            {
                "job": job,
                "ev": evaluation,
                "cv_options": cv_options,
                "events": events,
                "new_since_cutoff": new_since_cutoff,
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
            new_since_cutoff = get_new_since_cutoff(session)
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
                        "new_since_cutoff": new_since_cutoff,
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
                    "new_since_cutoff": new_since_cutoff,
                    "threshold": resolved_cfg.search.shortlist_threshold,
                    "current_path": safe_local_redirect(next_url, fallback="/"),
                },
                headers=headers,
            )

        target = safe_local_redirect(next_url, fallback=f"/jobs/{job.id}")
        return RedirectResponse(url=target, status_code=303)

    @app.post("/jobs/{job_id}/status", dependencies=[Depends(verify_csrf)])
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

    @app.post("/jobs/{job_id}/cv", dependencies=[Depends(verify_csrf)])
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

    @app.post("/jobs/{job_id}/notes", dependencies=[Depends(verify_csrf)])
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
