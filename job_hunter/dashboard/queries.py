"""Database queries, read-only `explain` evaluation, CSV export, and logged mutations."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from job_hunter.config import AppConfig, CVEntry
from job_hunter.core.cv_loader import CVRecord
from job_hunter.core.db import log_event
from job_hunter.core.models import STATUSES, CVVersion, Event, Job, Run, utcnow
from job_hunter.core.pipeline import Evaluation, Evaluator
from job_hunter.core.skills import SkillMatcher

SORT_KEYS = ("date", "company", "title", "platform", "score", "status")
DEFAULT_SORT = "score"
DEFAULT_ORDER = "desc"
DEFAULT_PER_PAGE = 25
MAX_PER_PAGE = 100

CSV_COLUMNS = (
    "id",
    "first_seen_at",
    "company",
    "title",
    "source",
    "location",
    "match_score",
    "status",
    "cv_id",
    "status_reason",
    "notes",
    "url",
)


class JobNotFoundError(LookupError):
    """Raised when a target job row does not exist."""


class InvalidInputError(ValueError):
    """Raised when a form mutation receives an invalid status or unknown CV id."""


@dataclass(frozen=True)
class CVOption:
    id: str
    label: str
    is_default: bool


@dataclass(frozen=True)
class JobFilterParams:
    status: str = ""
    platform: str = ""
    min_score: int | None = None
    max_score: int | None = None
    date_from: date | None = None
    date_to: date | None = None
    q: str = ""
    sort: str = DEFAULT_SORT
    order: str = DEFAULT_ORDER
    page: int = 1
    per_page: int = DEFAULT_PER_PAGE

    def query_items(
        self,
        *,
        include_sort: bool = True,
        include_pagination: bool = False,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        data: dict[str, Any] = {}
        if self.status:
            data["status"] = self.status
        if self.platform:
            data["platform"] = self.platform
        if self.min_score is not None:
            data["min_score"] = str(self.min_score)
        if self.max_score is not None:
            data["max_score"] = str(self.max_score)
        if self.date_from is not None:
            data["date_from"] = self.date_from.isoformat()
        if self.date_to is not None:
            data["date_to"] = self.date_to.isoformat()
        if self.q:
            data["q"] = self.q
        if include_sort:
            if self.sort != DEFAULT_SORT or self.order != DEFAULT_ORDER:
                data["sort"] = self.sort
                data["order"] = self.order
        if include_pagination:
            if self.page > 1:
                data["page"] = str(self.page)
            if self.per_page != DEFAULT_PER_PAGE:
                data["per_page"] = str(self.per_page)
        if overrides:
            for k, v in overrides.items():
                if v is None or v == "":
                    data.pop(k, None)
                else:
                    data[k] = str(v)
        return {k: str(v) for k, v in data.items()}

    def query_string(
        self,
        *,
        include_sort: bool = True,
        include_pagination: bool = False,
        overrides: dict[str, Any] | None = None,
    ) -> str:
        items = self.query_items(
            include_sort=include_sort,
            include_pagination=include_pagination,
            overrides=overrides,
        )
        return urlencode(items)


def _parse_int(raw: str | None) -> int | None:
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _parse_date(raw: str | None) -> date | None:
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def parse_filter_params(
    *,
    status: str | None = None,
    platform: str | None = None,
    min_score: str | None = None,
    max_score: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
    sort: str | None = None,
    order: str | None = None,
    page: str | None = None,
    per_page: str | None = None,
) -> JobFilterParams:
    clean_status = (status or "").strip()
    clean_platform = (platform or "").strip()
    clean_q = (q or "").strip()

    clean_sort = (sort or "").strip().lower()
    if clean_sort not in SORT_KEYS:
        clean_sort = DEFAULT_SORT

    clean_order = (order or "").strip().lower()
    if clean_order not in ("asc", "desc"):
        clean_order = DEFAULT_ORDER

    parsed_page = _parse_int(page)
    clean_page = parsed_page if parsed_page is not None and parsed_page >= 1 else 1

    parsed_per_page = _parse_int(per_page)
    if parsed_per_page is None or parsed_per_page < 1:
        clean_per_page = DEFAULT_PER_PAGE
    else:
        clean_per_page = min(MAX_PER_PAGE, parsed_per_page)

    return JobFilterParams(
        status=clean_status,
        platform=clean_platform,
        min_score=_parse_int(min_score),
        max_score=_parse_int(max_score),
        date_from=_parse_date(date_from),
        date_to=_parse_date(date_to),
        q=clean_q,
        sort=clean_sort,
        order=clean_order,
        page=clean_page,
        per_page=clean_per_page,
    )


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def build_jobs_query(params: JobFilterParams) -> Select[tuple[Job]]:
    """Build a filtered and sorted SQLAlchemy Select for `Job` (unpaginated)."""
    stmt = select(Job)

    if params.status:
        stmt = stmt.where(Job.status == params.status)
    if params.platform:
        stmt = stmt.where(Job.source == params.platform)
    if params.min_score is not None:
        stmt = stmt.where(Job.match_score >= params.min_score)
    if params.max_score is not None:
        stmt = stmt.where(Job.match_score <= params.max_score)
    if params.date_from is not None:
        start_dt = datetime.combine(params.date_from, time.min)
        stmt = stmt.where(Job.first_seen_at >= start_dt)
    if params.date_to is not None:
        end_exclusive = datetime.combine(params.date_to + timedelta(days=1), time.min)
        stmt = stmt.where(Job.first_seen_at < end_exclusive)
    if params.q:
        pattern = f"%{_escape_like(params.q)}%"
        stmt = stmt.where(
            or_(
                func.coalesce(Job.title, "").ilike(pattern, escape="\\"),
                func.coalesce(Job.company, "").ilike(pattern, escape="\\"),
                func.coalesce(Job.description_text, "").ilike(pattern, escape="\\"),
                func.coalesce(Job.location, "").ilike(pattern, escape="\\"),
                func.coalesce(Job.status_reason, "").ilike(pattern, escape="\\"),
                func.coalesce(Job.notes, "").ilike(pattern, escape="\\"),
            )
        )

    asc = params.order == "asc"
    if params.sort == "date":
        primary = Job.first_seen_at.asc() if asc else Job.first_seen_at.desc()
    elif params.sort == "company":
        col = func.lower(Job.company)
        primary = col.asc() if asc else col.desc()
    elif params.sort == "title":
        col = func.lower(Job.title)
        primary = col.asc() if asc else col.desc()
    elif params.sort == "platform":
        col = func.lower(Job.source)
        primary = col.asc() if asc else col.desc()
    elif params.sort == "status":
        primary = Job.status.asc() if asc else Job.status.desc()
    else:  # "score"
        primary = Job.match_score.asc().nulls_last() if asc else Job.match_score.desc().nulls_last()

    stmt = stmt.order_by(primary, Job.first_seen_at.desc(), Job.id.desc())
    return stmt


def query_jobs_page(session: Session, params: JobFilterParams) -> tuple[list[Job], int, int]:
    """Return `(jobs_on_page, total_matching, total_pages)`."""
    base = build_jobs_query(params)
    count_stmt = select(func.count()).select_from(base.order_by(None).subquery())
    total = int(session.scalar(count_stmt) or 0)
    total_pages = max(1, (total + params.per_page - 1) // params.per_page)
    effective_page = min(params.page, total_pages)
    offset = (effective_page - 1) * params.per_page
    jobs = list(session.scalars(base.offset(offset).limit(params.per_page)).all())
    return jobs, total, total_pages


def get_status_counts(session: Session) -> tuple[dict[str, int], int]:
    """Return `(counts_for_all_10_statuses, total_jobs)`."""
    raw = dict(session.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    counts = {st: int(raw.get(st, 0)) for st in STATUSES}
    return counts, sum(counts.values())


def get_platforms(session: Session) -> list[str]:
    rows = session.scalars(select(Job.source).distinct().order_by(func.lower(Job.source))).all()
    return [r for r in rows if r]


def get_recent_runs(session: Session, limit: int = 8) -> list[Run]:
    return list(session.scalars(select(Run).order_by(Run.id.desc()).limit(limit)).all())


def get_cv_options(session: Session, cfg: AppConfig | None = None) -> list[CVOption]:
    label_by_id: dict[str, str] = {}
    if cfg is not None:
        label_by_id = {entry.id: entry.label for entry in cfg.cvs}
    rows = session.scalars(select(CVVersion).order_by(CVVersion.is_default.desc(), CVVersion.id.asc())).all()
    options: list[CVOption] = []
    for row in rows:
        cfg_label = label_by_id.get(row.id)
        display = f"{cfg_label} ({row.id})" if cfg_label and cfg_label != row.id else row.id
        options.append(CVOption(id=row.id, label=display, is_default=row.is_default))
    return options


def build_readonly_evaluator(session: Session, cfg: AppConfig, matcher: SkillMatcher) -> Evaluator:
    """Construct an `Evaluator` from DB `cv_versions` + `cfg` without mutating DB or re-reading PDFs."""
    cfg_entries: dict[str, CVEntry] = {entry.id: entry for entry in cfg.cvs}
    rows = session.scalars(select(CVVersion).order_by(CVVersion.id.asc())).all()
    records: list[CVRecord] = []
    seen_ids: set[str] = set()

    for row in rows:
        seen_ids.add(row.id)
        entry = cfg_entries.get(row.id)
        skills = list(row.skills_json or [])
        records.append(
            CVRecord(
                id=row.id,
                file=row.file,
                label=entry.label if entry else row.id,
                tags=list(entry.tags) if entry else [],
                default=entry.default if entry else row.is_default,
                skills=skills,
                ok=bool(skills),
            )
        )

    for entry in cfg.cvs:
        if entry.id not in seen_ids:
            records.append(
                CVRecord(
                    id=entry.id,
                    file=entry.file,
                    label=entry.label,
                    tags=list(entry.tags),
                    default=entry.default,
                    skills=[],
                    ok=False,
                )
            )

    return Evaluator(cfg, records, matcher)


def explain_job(session: Session, job: Job, cfg: AppConfig, matcher: SkillMatcher) -> Evaluation:
    evaluator = build_readonly_evaluator(session, cfg, matcher)
    return evaluator.evaluate_job(job)


def get_job_events(session: Session, job_id: int, limit: int = 20) -> list[Event]:
    stmt = select(Event).where(Event.job_id == job_id).order_by(Event.id.desc()).limit(limit)
    return list(session.scalars(stmt).all())


def export_jobs_csv(session: Session, params: JobFilterParams) -> str:
    """Render all jobs matching `params` (ignoring pagination) as UTF-8 CSV text."""
    jobs = session.scalars(build_jobs_query(params)).all()
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for j in jobs:
        writer.writerow([
            j.id,
            j.first_seen_at.strftime("%Y-%m-%d %H:%M:%S") if j.first_seen_at else "",
            j.company,
            j.title,
            j.source,
            j.location,
            "" if j.match_score is None else j.match_score,
            j.status,
            j.cv_id or "",
            j.status_reason,
            j.notes,
            j.url or "",
        ])
    return buf.getvalue()


# --------------------------------------------------------------------------- Logged mutations


def set_job_status(session: Session, job_id: int, new_status: str) -> Job:
    clean = (new_status or "").strip()
    if clean not in STATUSES:
        raise InvalidInputError(f"Invalid status: {new_status!r}")
    job = session.get(Job, job_id)
    if job is None:
        raise JobNotFoundError(f"No job with id {job_id}")
    if job.status != clean:
        old = job.status
        job.status = clean
        job.updated_at = utcnow()
        log_event(session, "status_changed", f"{old} -> {clean}", job_id=job.id)
    return job


def set_job_cv(session: Session, job_id: int, raw_cv_id: str | None) -> Job:
    clean = (raw_cv_id or "").strip() or None
    job = session.get(Job, job_id)
    if job is None:
        raise JobNotFoundError(f"No job with id {job_id}")
    if clean is not None and session.get(CVVersion, clean) is None:
        raise InvalidInputError(f"Unknown CV id: {clean!r}")
    if job.cv_id != clean:
        old = job.cv_id
        job.cv_id = clean
        job.updated_at = utcnow()
        log_event(session, "cv_changed", f"{old or '-'} -> {clean or '-'}", job_id=job.id)
    return job


def set_job_notes(session: Session, job_id: int, notes: str) -> Job:
    clean = (notes or "").replace("\r\n", "\n")
    job = session.get(Job, job_id)
    if job is None:
        raise JobNotFoundError(f"No job with id {job_id}")
    if job.notes != clean:
        job.notes = clean
        job.updated_at = utcnow()
        log_event(session, "notes_updated", f"notes updated ({len(clean)} chars)", job_id=job.id)
    return job
