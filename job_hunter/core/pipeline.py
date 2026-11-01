"""Ingest -> dedup -> filter -> score -> status. Idempotent and fully logged to `events`."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from job_hunter.config import AppConfig
from job_hunter.core.categories import Categorizer, CategoryMatch
from job_hunter.core.cv_loader import CVRecord, suggest_cv
from job_hunter.core.db import log_event
from job_hunter.core.dedup import fingerprint, normalize_url
from job_hunter.core.filters import FilterDecision, JobFilters
from job_hunter.core.models import ENGINE_STATUSES, Job, Run, utcnow
from job_hunter.core.scoring import ScoreBreakdown, Scorer
from job_hunter.core.skills import SkillMatcher
from job_hunter.core.text import detect_language

log = logging.getLogger("job_hunter.pipeline")


@dataclass
class JobPosting:
    """Normalized input for the pipeline (scrapers convert RawJob/JobDetail into this)."""

    source: str
    title: str
    company: str = ""
    location: str = ""
    description_text: str = ""
    url: str | None = None
    apply_url: str | None = None
    source_job_id: str | None = None
    posted_at: datetime | None = None


@dataclass
class Evaluation:
    decision: FilterDecision
    category: CategoryMatch
    breakdown: ScoreBreakdown
    job_skills: list[str]
    matched_skills: list[str]
    cv_id: str | None
    status: str
    status_reason: str

    @property
    def score(self) -> int:
        return self.breakdown.total


class Evaluator:
    """Pure decision logic (no DB writes)."""

    def __init__(self, cfg: AppConfig, cv_records: list[CVRecord], skill_matcher: SkillMatcher | None = None):
        self.cfg = cfg
        self.skill_matcher = skill_matcher or SkillMatcher(cfg.skills)
        self.filters = JobFilters(cfg.search)
        self.categorizer = Categorizer(cfg.search)
        self.scorer = Scorer(cfg.search, self.skill_matcher)
        self.cv_records = cv_records
        self.core_skills = self.skill_matcher.canonicalize_all(cfg.profile.core_skills)

    def candidate_skills(self, cv: CVRecord | None) -> set[str]:
        skills = set(self.core_skills)
        if cv is not None and cv.ok:
            skills |= set(cv.skills)
        return skills

    def _pick_cv(self, current_cv_id: str | None, category: CategoryMatch) -> CVRecord | None:
        if current_cv_id:  # keep an existing (possibly user-overridden) choice
            for r in self.cv_records:
                if r.id == current_cv_id:
                    return r
        return suggest_cv(category.category.tag, self.cv_records)

    def evaluate(self, *, title: str, description: str, company: str = "", posted_at: datetime | None = None,
                 current_cv_id: str | None = None, now: datetime | None = None) -> Evaluation:
        now = now or utcnow()
        decision = self.filters.evaluate(title, description, company=company)
        category = self.categorizer.categorize(title, description)
        cv = self._pick_cv(current_cv_id, category)
        breakdown, job_skills, matched = self.scorer.score(
            title=title, description=description, company=company, posted_at=posted_at,
            category=category, years_min=decision.years_min,
            candidate_skills=self.candidate_skills(cv), now=now,
        )
        threshold = self.cfg.search.shortlist_threshold
        cat = category.category.name
        if decision.skip:
            status, reason = "Skipped", f"{decision.reason} [{cat}]"
        elif breakdown.total >= threshold:
            status = "Shortlisted"
            reason = f"Shortlisted - Score {breakdown.total} >= {threshold} [{cat}] ({breakdown.summary()})"
        else:
            status = "New"
            reason = f"New - Score {breakdown.total} below threshold {threshold} [{cat}] ({breakdown.summary()})"
        return Evaluation(decision, category, breakdown, job_skills, matched, cv.id if cv else None,
                          status, reason)

    def evaluate_job(self, job: Job, now: datetime | None = None) -> Evaluation:
        return self.evaluate(title=job.title, description=job.description_text, company=job.company,
                             posted_at=job.posted_at, current_cv_id=job.cv_id, now=now)


# ----------------------------------------------------------------------------- DB operations

def find_duplicate(session: Session, fp: str, source: str, source_job_id: str | None,
                   url_norm: str | None) -> tuple[Job | None, str | None]:
    conds = [Job.fingerprint == fp]
    if source_job_id:
        conds.append((Job.source == source) & (Job.source_job_id == source_job_id))
    if url_norm:
        conds.append(Job.url_normalized == url_norm)
    job = session.scalars(select(Job).where(or_(*conds)).limit(1)).first()
    if job is None:
        return None, None
    if job.fingerprint == fp:
        return job, "fingerprint"
    if source_job_id and job.source == source and job.source_job_id == source_job_id:
        return job, "source_job_id"
    return job, "url"


def ingest(session: Session, posting: JobPosting, now: datetime | None = None) -> tuple[Job, bool]:
    """Insert a posting unless it is a duplicate. Duplicates only get last_seen_at bumped."""
    now = now or utcnow()
    fp = fingerprint(posting.company, posting.title, posting.location, posting.source_job_id)
    url_norm = normalize_url(posting.url)
    existing, key = find_duplicate(session, fp, posting.source, posting.source_job_id, url_norm)
    if existing is not None:
        existing.last_seen_at = now
        log_event(session, "job_duplicate",
                  f"{posting.source}:{posting.source_job_id or '-'} matches job {existing.id} by {key}",
                  job_id=existing.id)
        return existing, False

    job = Job(
        fingerprint=fp, source=posting.source, source_job_id=posting.source_job_id,
        url=posting.url, url_normalized=url_norm, apply_url=posting.apply_url,
        company=posting.company or "", title=posting.title, location=posting.location or "",
        description_text=posting.description_text or "",
        language=detect_language(f"{posting.title}\n{posting.description_text}"),
        posted_at=posting.posted_at, first_seen_at=now, last_seen_at=now,
        status="New", status_reason="New - Not processed yet",
    )
    session.add(job)
    session.flush()
    log_event(session, "job_inserted", f"{posting.source}: {posting.title} @ {posting.company}", job_id=job.id)
    return job, True


def apply_evaluation(session: Session, job: Job, ev: Evaluation, action: str | None = None) -> None:
    job.years_required_min = ev.decision.years_min
    job.matched_skills = ev.matched_skills
    job.match_score = ev.score
    job.cv_id = ev.cv_id
    job.status = ev.status
    job.status_reason = ev.status_reason
    job.updated_at = utcnow()
    if action is None:
        action = {"Skipped": "job_skipped", "Shortlisted": "job_shortlisted"}.get(ev.status, "job_scored")
    log_event(session, action, ev.status_reason, job_id=job.id)


def process_job(session: Session, evaluator: Evaluator, job: Job, now: datetime | None = None) -> Evaluation:
    ev = evaluator.evaluate_job(job, now=now)
    apply_evaluation(session, job, ev)
    return ev


def process_pending(session: Session, evaluator: Evaluator, now: datetime | None = None) -> dict[str, int]:
    """Filter + score every job not processed yet (match_score IS NULL)."""
    counts = {"processed": 0, "Skipped": 0, "Shortlisted": 0, "New": 0}
    for job in session.scalars(select(Job).where(Job.match_score.is_(None)).order_by(Job.id)).all():
        ev = process_job(session, evaluator, job, now)
        counts["processed"] += 1
        counts[ev.status] += 1
    return counts


def rescore(session: Session, evaluator: Evaluator, now: datetime | None = None) -> dict[str, int]:
    """Re-run filter + score for engine-owned statuses only (New/Skipped/Shortlisted)."""
    counts = {"checked": 0, "changed": 0}
    jobs = session.scalars(select(Job).where(Job.status.in_(ENGINE_STATUSES)).order_by(Job.id)).all()
    for job in jobs:
        counts["checked"] += 1
        before = (job.status, job.match_score)
        ev = evaluator.evaluate_job(job, now=now)
        if before != (ev.status, ev.score) or job.status_reason != ev.status_reason:
            counts["changed"] += 1
            apply_evaluation(session, job, ev, action="job_rescored")
    return counts


def run_postings(session: Session, evaluator: Evaluator, postings: list[JobPosting], site: str,
                 now: datetime | None = None) -> Run:
    """Ingest + process a batch as one tracked run (row in `runs`). Per-posting errors don't abort the run."""
    run = Run(site=site, started_at=utcnow(), errors=[])
    session.add(run)
    session.flush()
    log_event(session, "run_started", f"run {run.id} site={site} postings={len(postings)}")
    errors: list[str] = []
    for posting in postings:
        try:
            with session.begin_nested():
                job, created = ingest(session, posting, now)
                if not created:
                    continue
                run.new_count += 1
                ev = process_job(session, evaluator, job, now)
                if ev.status == "Skipped":
                    run.skipped_count += 1
                elif ev.status == "Shortlisted":
                    run.shortlisted_count += 1
        except Exception as exc:  # noqa: BLE001 - record and continue
            msg = f"{posting.source}:{posting.source_job_id or '-'} {exc.__class__.__name__}: {exc}"
            errors.append(msg)
            log.warning("posting failed: %s", msg)
            log_event(session, "job_error", msg)
    run.errors = errors
    run.finished_at = utcnow()
    log_event(session, "run_finished",
              f"run {run.id}: new={run.new_count} skipped={run.skipped_count} "
              f"shortlisted={run.shortlisted_count} errors={len(errors)}")
    return run
