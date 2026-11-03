"""Scrape & offline-HTML-import orchestration feeding `job_hunter.core.pipeline`.

Responsibilities:
- Enforce per-site `enabled` flag and log page-budget warnings.
- Fetch & cache `robots.txt` at run start; stop immediately if unavailable.
- Stop immediately on CAPTCHA / HTTP 401/403/429 / login wall / disallowed redirect (`BlockedError` /
  `RobotsDisallowedError`), recording the stop reason in `events` and `runs.errors`.
- Deduplicate postings across queries within a run, then ingest + evaluate via `core.pipeline`.
- Support `--dry-run` (evaluates in memory + checks existing DB duplicates read-only; zero DB writes).
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.orm import Session, sessionmaker

import job_hunter.core.cv_loader as cv_loader
from job_hunter.config import AppConfig
from job_hunter.core.cv_loader import CVRecord, sha256_file, sync_cvs
from job_hunter.core.db import log_event, session_scope
from job_hunter.core.dedup import fingerprint, normalize_url
from job_hunter.core.models import CVVersion, Run, utcnow
from job_hunter.core.pipeline import (
    Evaluation,
    Evaluator,
    JobPosting,
    find_duplicate,
    ingest,
    process_job,
)
from job_hunter.core.skills import SkillMatcher
from job_hunter.scrapers.alljobs import build_alljobs_http_settings
from job_hunter.scrapers.base import BaseScraper, to_posting
from job_hunter.scrapers.http import (
    DEFAULT_USER_AGENT,
    BlockedError,
    HttpSettings,
    PoliteHttpClient,
    ScanStoppedError,
    ScraperError,
)
from job_hunter.scrapers.registry import get_scraper
from job_hunter.scrapers.robots import GLOBAL_HARD_DENY_PATH_PREFIXES, RobotsDisallowedError, RobotsUnavailableError

log = logging.getLogger("job_hunter.scrapers.runner")


class SiteDisabledError(RuntimeError):
    """Requested site is missing or has `enabled: false` in config/search.yaml."""


@dataclass
class PreviewItem:
    posting: JobPosting
    evaluation: Evaluation | None
    duplicate_of_id: int | None = None
    job_id: int | None = None


@dataclass(frozen=True)
class SiteProgress:
    site: str
    query_index: int
    queries_total: int
    current_query: str | None
    pages_fetched: int
    jobs_found: int


@dataclass
class ScrapeSummary:
    site: str
    dry_run: bool
    run_id: int | None = None
    pages_fetched: int = 0
    queries_run: int = 0
    queries_planned: int = 0
    unique_postings: int = 0
    new_count: int = 0
    duplicate_count: int = 0
    shortlisted_count: int = 0
    skipped_count: int = 0
    below_threshold_count: int = 0
    aborted: bool = False
    blocked: bool = False
    stopped: bool = False
    abort_reason: str | None = None
    errors: list[str] = field(default_factory=list)
    previews: list[PreviewItem] = field(default_factory=list)


def _load_cvs_readonly(session: Session, cfg: AppConfig, matcher: SkillMatcher) -> list[CVRecord]:
    """Load CV skills without writing to `cv_versions` or `events` (used by `--dry-run`)."""
    records: list[CVRecord] = []
    for entry in cfg.cvs:
        rec = CVRecord(entry.id, entry.file, entry.label, entry.tags, entry.default)
        path = Path(entry.file)
        if not path.is_absolute():
            path = cfg.root / path
        if not path.exists():
            rec.problem = "file not found"
            records.append(rec)
            continue
        try:
            digest = sha256_file(path)
        except OSError as exc:
            rec.problem = f"cannot read file ({exc.__class__.__name__})"
            records.append(rec)
            continue

        row = session.get(CVVersion, entry.id)
        if row is not None and row.sha256 == digest and row.file == entry.file:
            rec.skills = list(row.skills_json or [])
            rec.ok = len((row.extracted_text or "").strip()) >= cv_loader.MIN_TEXT_CHARS and bool(rec.skills)
            records.append(rec)
            continue

        text = cv_loader.extract_pdf_text(path) or ""
        rec.skills = matcher.extract(text)
        rec.ok = len(text.strip()) >= cv_loader.MIN_TEXT_CHARS and bool(rec.skills)
        records.append(rec)
    return records


def _build_http_settings(
    site: str,
    site_cfg: dict[str, Any],
    scraper_cls: type[BaseScraper] | None = None,
) -> HttpSettings:
    if scraper_cls is not None and hasattr(scraper_cls, "build_http_settings"):
        return scraper_cls.build_http_settings(site_cfg)  # type: ignore[no-any-return]
    if site == "alljobs":
        return build_alljobs_http_settings(site_cfg)
    default_origin = getattr(scraper_cls, "origin", "") if scraper_cls is not None else ""
    origin = str(site_cfg.get("origin") or default_origin or "")
    if not origin:
        raise ValueError(f"Site '{site}' must specify 'origin' for HTTP settings")
    return HttpSettings(
        origin=origin,
        user_agent=str(site_cfg.get("user_agent") or DEFAULT_USER_AGENT),
        delay_min_seconds=float(site_cfg.get("delay_min_seconds", 3.0)),
        delay_max_seconds=float(site_cfg.get("delay_max_seconds", 8.0)),
        max_pages_per_run=int(site_cfg.get("max_pages_per_run", 40)),
        max_retries=int(site_cfg.get("max_retries", 2)),
        deny_path_prefixes=GLOBAL_HARD_DENY_PATH_PREFIXES,
    )


def _instantiate_scraper(
    scraper_cls: type[BaseScraper],
    site_cfg: dict[str, Any],
    *,
    http_client: PoliteHttpClient | None,
    now_fn: Callable[[], datetime],
    sleep_fn: Callable[[float], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> BaseScraper:
    sig = inspect.signature(scraper_cls.__init__)
    params = sig.parameters
    has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    kwargs: dict[str, Any] = {}
    if http_client is not None and ("http_client" in params or has_varkw):
        kwargs["http_client"] = http_client
    if "now_fn" in params or has_varkw:
        kwargs["now_fn"] = now_fn
    if sleep_fn is not None and ("sleep_fn" in params or has_varkw):
        kwargs["sleep_fn"] = sleep_fn
    if should_stop is not None and ("should_stop" in params or has_varkw):
        kwargs["should_stop"] = should_stop
    return scraper_cls(site_cfg, **kwargs)


def check_page_budget(site: str, queries: Sequence[str], site_cfg: dict[str, Any]) -> str | None:
    """Return a warning message (and log it) if planned query*pages exceeds max_pages_per_run."""
    per_query = max(1, int(site_cfg.get("max_pages_per_query", 2)))
    per_run = max(1, int(site_cfg.get("max_pages_per_run", 40)))
    planned = len(queries) * per_query
    if planned > per_run:
        msg = (
            f"Site '{site}' max_pages_per_run ({per_run}) cannot cover all {len(queries)} queries "
            f"at {per_query} pages/query (planned={planned}); trailing queries may be skipped"
        )
        log.warning(msg)
        return msg
    return None


def _evaluate_or_persist(
    session: Session,
    evaluator: Evaluator,
    postings: list[JobPosting],
    summary: ScrapeSummary,
    *,
    now: datetime,
) -> None:
    for posting in postings:
        if summary.dry_run:
            fp = fingerprint(posting.company, posting.title, posting.location, posting.source_job_id)
            url_norm = normalize_url(posting.url)
            existing, _key = find_duplicate(session, fp, posting.source, posting.source_job_id, url_norm)
            if existing is not None:
                summary.duplicate_count += 1
                summary.previews.append(PreviewItem(posting=posting, evaluation=None, duplicate_of_id=existing.id))
                continue
            ev = evaluator.evaluate(
                title=posting.title,
                description=posting.description_text,
                company=posting.company,
                posted_at=posting.posted_at,
                now=now,
            )
            summary.new_count += 1
            if ev.status == "Shortlisted":
                summary.shortlisted_count += 1
            elif ev.status == "Skipped":
                summary.skipped_count += 1
            else:
                summary.below_threshold_count += 1
            summary.previews.append(PreviewItem(posting=posting, evaluation=ev))
            continue

        try:
            with session.begin_nested():
                job, created = ingest(session, posting, now)
                if not created:
                    summary.duplicate_count += 1
                    continue
                summary.new_count += 1
                ev = process_job(session, evaluator, job, now)
                if ev.status == "Skipped":
                    summary.skipped_count += 1
                elif ev.status == "Shortlisted":
                    summary.shortlisted_count += 1
                else:
                    summary.below_threshold_count += 1
                summary.previews.append(PreviewItem(posting=posting, evaluation=ev, job_id=job.id))
        except Exception as exc:  # noqa: BLE001 - isolate bad posting without aborting run
            msg = f"{posting.source}:{posting.source_job_id or '-'} {exc.__class__.__name__}: {exc}"
            summary.errors.append(msg)
            log.warning("posting failed: %s", msg)
            log_event(session, "job_error", msg)


def scrape_site(
    factory: sessionmaker[Session],
    cfg: AppConfig,
    matcher: SkillMatcher,
    site: str,
    *,
    dry_run: bool = False,
    queries: Sequence[str] | None = None,
    max_pages_override: int | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    now: datetime | None = None,
    on_progress: Callable[[SiteProgress], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> ScrapeSummary:
    """Execute a full scrape run for `site` (or preview in memory when `dry_run=True`)."""
    scraper_cls: type[BaseScraper] = get_scraper(site)
    raw_site_cfg = dict(cfg.search.sites.get(site) or {})
    if raw_site_cfg.get("enabled") is not True:
        raise SiteDisabledError(f"Site '{site}' is disabled in config/search.yaml (set sites.{site}.enabled: true)")

    if max_pages_override is not None:
        if max_pages_override < 1:
            raise ValueError("--max-pages must be >= 1")
        raw_site_cfg["max_pages_per_run"] = max_pages_override
        raw_site_cfg["max_pages_per_query"] = min(int(raw_site_cfg.get("max_pages_per_query", 2)), max_pages_override)

    active_queries = [q.strip() for q in (queries if queries is not None else cfg.search.roles) if q and q.strip()]
    ref_now = now or utcnow()
    summary = ScrapeSummary(site=site, dry_run=dry_run, queries_planned=len(active_queries))

    budget_warning = check_page_budget(site, active_queries, raw_site_cfg)

    cv_records: list[CVRecord] = []
    if not dry_run:
        with session_scope(factory) as s:
            cv_records = sync_cvs(s, cfg, matcher)
            run = Run(site=site, started_at=ref_now, errors=[])
            s.add(run)
            s.flush()
            summary.run_id = run.id
            log_event(
                s,
                "run_started",
                f"run {run.id} site={site} queries_planned={summary.queries_planned}",
            )
            if budget_warning:
                log_event(s, "page_budget_warning", budget_warning)

    buffered_events: list[tuple[str, str]] = []

    def _record_http_event(action: str, result: str) -> None:
        buffered_events.append((action, result))

    postings: list[JobPosting] = []
    seen_keys: set[str] = set()
    current_q_text: str | None = None

    def _emit_progress(pages: int) -> None:
        if on_progress is not None:
            on_progress(
                SiteProgress(
                    site=site,
                    query_index=summary.queries_run,
                    queries_total=summary.queries_planned,
                    current_query=current_q_text,
                    pages_fetched=pages,
                    jobs_found=len(postings),
                )
            )

    def _check_stop() -> None:
        if should_stop is not None and should_stop():
            raise ScanStoppedError("Stopped by user")

    try:
        if getattr(scraper_cls, "requires_http", True):
            http_settings = _build_http_settings(site, raw_site_cfg, scraper_cls)
            effective_transport = transport if transport is not None else scraper_cls.build_transport(raw_site_cfg)
            raw_httpx = httpx.Client(
                timeout=http_settings.timeout_seconds,
                follow_redirects=False,
                transport=effective_transport,
            )
            http_kwargs: dict[str, Any] = {
                "client": raw_httpx,
                "on_event": _record_http_event,
                "should_stop": should_stop,
                "on_page_fetched": _emit_progress,
            }
            if sleep_fn is not None:
                http_kwargs["sleep_fn"] = sleep_fn

            try:
                with PoliteHttpClient(http_settings, **http_kwargs) as polite:
                    scraper = _instantiate_scraper(
                        scraper_cls,
                        raw_site_cfg,
                        http_client=polite,
                        now_fn=lambda: ref_now,
                        sleep_fn=sleep_fn,
                        should_stop=should_stop,
                    )
                    try:
                        _emit_progress(0)
                        polite.ensure_robots()
                        for q in active_queries:
                            _check_stop()
                            if polite.pages_fetched >= http_settings.max_pages_per_run:
                                break
                            summary.queries_run += 1
                            current_q_text = q
                            _emit_progress(polite.pages_fetched)
                            buffered_events.append(("search_query", f"{site}: query={q!r}"))
                            try:
                                cards = scraper.search(q)
                            except (BlockedError, RobotsDisallowedError, RobotsUnavailableError, ScanStoppedError):
                                raise
                            except ScraperError as exc:
                                msg = f"query {q!r}: {exc}"
                                summary.errors.append(msg)
                                buffered_events.append(("scrape_error", msg))
                                log.warning("query %r failed: %s", q, exc)
                                continue

                            for raw in cards:
                                dedup_key = raw.source_job_id or (normalize_url(raw.url) or raw.url)
                                if dedup_key in seen_keys:
                                    continue
                                seen_keys.add(dedup_key)
                                detail = scraper.fetch_detail(raw.url)
                                postings.append(to_posting(raw, detail))
                            _emit_progress(polite.pages_fetched)
                    except RobotsUnavailableError as exc:
                        summary.aborted = True
                        summary.blocked = True
                        summary.abort_reason = str(exc)
                        summary.errors.append(str(exc))
                        buffered_events.append(("robots_error", str(exc)))
                        log.error("robots.txt unavailable for %s: %s", site, exc)
                    except (BlockedError, RobotsDisallowedError) as exc:
                        summary.aborted = True
                        summary.blocked = True
                        summary.abort_reason = str(exc)
                        summary.errors.append(str(exc))
                        action_name = "robots_denied" if isinstance(exc, RobotsDisallowedError) else "scrape_blocked"
                        if not any(a == action_name and r == str(exc) for a, r in buffered_events):
                            buffered_events.append((action_name, str(exc)))
                        log.error("scrape stopped for %s: %s", site, exc)
                    except ScanStoppedError as exc:
                        summary.stopped = True
                        summary.abort_reason = str(exc)
                        summary.errors.append(str(exc))
                        buffered_events.append(("scan_stopped", f"{site}: {exc}"))
                        log.info("scrape stopped by user for %s", site)
                    finally:
                        summary.pages_fetched = polite.pages_fetched
            finally:
                raw_httpx.close()
        else:
            max_pages = int(raw_site_cfg.get("max_pages_per_run", 40))
            scraper = _instantiate_scraper(
                scraper_cls,
                raw_site_cfg,
                http_client=None,
                now_fn=lambda: ref_now,
                sleep_fn=sleep_fn,
                should_stop=should_stop,
            )
            try:
                _emit_progress(0)
                for q in active_queries:
                    _check_stop()
                    pages_now = int(getattr(scraper, "pages_fetched", summary.pages_fetched))
                    if pages_now >= max_pages:
                        break
                    summary.queries_run += 1
                    current_q_text = q
                    _emit_progress(pages_now)
                    buffered_events.append(("search_query", f"{site}: query={q!r}"))
                    try:
                        cards = scraper.search(q)
                    except (BlockedError, RobotsDisallowedError, RobotsUnavailableError, ScanStoppedError):
                        raise
                    except ScraperError as exc:
                        msg = f"query {q!r}: {exc}"
                        summary.errors.append(msg)
                        buffered_events.append(("scrape_error", msg))
                        continue

                    for raw in cards:
                        dedup_key = raw.source_job_id or (normalize_url(raw.url) or raw.url)
                        if dedup_key in seen_keys:
                            continue
                        seen_keys.add(dedup_key)
                        detail = scraper.fetch_detail(raw.url)
                        postings.append(to_posting(raw, detail))
                    summary.pages_fetched = int(getattr(scraper, "pages_fetched", summary.queries_run))
                    _emit_progress(summary.pages_fetched)
            except RobotsUnavailableError as exc:
                summary.aborted = True
                summary.blocked = True
                summary.abort_reason = str(exc)
                summary.errors.append(str(exc))
                buffered_events.append(("robots_error", str(exc)))
            except (BlockedError, RobotsDisallowedError) as exc:
                summary.aborted = True
                summary.blocked = True
                summary.abort_reason = str(exc)
                summary.errors.append(str(exc))
                action_name = "robots_denied" if isinstance(exc, RobotsDisallowedError) else "scrape_blocked"
                buffered_events.append((action_name, str(exc)))
            except ScanStoppedError as exc:
                summary.stopped = True
                summary.abort_reason = str(exc)
                summary.errors.append(str(exc))
                buffered_events.append(("scan_stopped", f"{site}: {exc}"))

        summary.unique_postings = len(postings)

        if dry_run:
            s = factory()
            try:
                evaluator = Evaluator(cfg, _load_cvs_readonly(s, cfg, matcher), matcher)
                if not summary.stopped:
                    _evaluate_or_persist(s, evaluator, postings, summary, now=ref_now)
                s.rollback()
            finally:
                s.close()
            return summary

        with session_scope(factory) as s:
            evaluator = Evaluator(cfg, cv_records, matcher)
            run_row = s.get(Run, summary.run_id) if summary.run_id is not None else None
            if run_row is None:
                run_row = Run(site=site, started_at=ref_now, errors=[])
                s.add(run_row)
                s.flush()
                summary.run_id = run_row.id
            for action, result in buffered_events:
                log_event(s, action, result)

            if not summary.stopped:
                _evaluate_or_persist(s, evaluator, postings, summary, now=ref_now)

            run_row.new_count = summary.new_count
            run_row.skipped_count = summary.skipped_count
            run_row.shortlisted_count = summary.shortlisted_count
            run_row.errors = list(summary.errors)
            run_row.finished_at = utcnow()
            log_event(
                s,
                "run_finished",
                f"run {run_row.id}: pages={summary.pages_fetched} new={run_row.new_count} "
                f"skipped={run_row.skipped_count} shortlisted={run_row.shortlisted_count} "
                f"aborted={summary.aborted} stopped={summary.stopped} errors={len(summary.errors)}",
            )
        return summary
    except BaseException as exc:
        if not dry_run and summary.run_id is not None:
            try:
                with session_scope(factory) as s:
                    run_row = s.get(Run, summary.run_id)
                    if run_row is not None and run_row.finished_at is None:
                        run_row.errors = [*list(run_row.errors or []), f"{exc.__class__.__name__}: {exc}"]
                        run_row.finished_at = utcnow()
                        log_event(s, "run_failed", f"run {run_row.id} ({site}): {exc.__class__.__name__}: {exc}")
            except Exception:  # noqa: BLE001 - best-effort cleanup before re-raising
                pass
        raise


def import_html_file(
    factory: sessionmaker[Session],
    cfg: AppConfig,
    matcher: SkillMatcher,
    site: str,
    html_path: Path,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> ScrapeSummary:
    """Parse a locally saved search-results HTML file with `site`'s parser and feed the pipeline."""
    scraper_cls = get_scraper(site)
    if not html_path.exists():
        raise FileNotFoundError(f"HTML file not found: {html_path}")

    html = html_path.read_text(encoding="utf-8", errors="replace")
    site_cfg = dict(cfg.search.sites.get(site) or {})
    ref_now = now or utcnow()

    # Offline parse only — pass a dummy non-network client if supported, then close immediately.
    dummy_client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(500)))
    try:
        polite = PoliteHttpClient(_build_http_settings(site, site_cfg), client=dummy_client)
        scraper = scraper_cls(site_cfg, http_client=polite, now_fn=lambda: ref_now)  # type: ignore[call-arg]
        raw_postings = scraper.parse_saved_html(html, now=ref_now)
    finally:
        dummy_client.close()

    # Deduplicate within the imported file itself by source_job_id / normalized URL.
    postings: list[JobPosting] = []
    seen: set[str] = set()
    for p in raw_postings:
        key = p.source_job_id or (normalize_url(p.url) or p.url or f"{p.company}|{p.title}|{p.location}")
        if key not in seen:
            seen.add(key)
            postings.append(p)

    summary = ScrapeSummary(
        site=site,
        dry_run=dry_run,
        pages_fetched=1,
        queries_run=1,
        queries_planned=1,
        unique_postings=len(postings),
    )

    if dry_run:
        s = factory()
        try:
            evaluator = Evaluator(cfg, _load_cvs_readonly(s, cfg, matcher), matcher)
            _evaluate_or_persist(s, evaluator, postings, summary, now=ref_now)
            s.rollback()
        finally:
            s.close()
        return summary

    with session_scope(factory) as s:
        evaluator = Evaluator(cfg, sync_cvs(s, cfg, matcher), matcher)
        run = Run(site=site, started_at=ref_now, errors=[])
        s.add(run)
        s.flush()
        summary.run_id = run.id
        log_event(s, "run_started", f"run {run.id} site={site} import_html={html_path.name} postings={len(postings)}")
        _evaluate_or_persist(s, evaluator, postings, summary, now=ref_now)
        run.new_count = summary.new_count
        run.skipped_count = summary.skipped_count
        run.shortlisted_count = summary.shortlisted_count
        run.errors = list(summary.errors)
        run.finished_at = utcnow()
        log_event(
            s,
            "run_finished",
            f"run {run.id}: import_html={html_path.name} new={run.new_count} "
            f"skipped={run.skipped_count} shortlisted={run.shortlisted_count} errors={len(summary.errors)}",
        )
    return summary
