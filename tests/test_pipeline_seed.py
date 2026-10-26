from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from job_hunter.core.models import Event, Job, Run
from job_hunter.core.pipeline import JobPosting, ingest, process_pending, rescore, run_postings
from job_hunter.seed_data import SEED_JOBS, seed_postings
from tests.conftest import NOW


def _seed(session, evaluator) -> Run:
    run = run_postings(session, evaluator, seed_postings(NOW), site="seed", now=NOW)
    session.commit()
    return run


def _by_key(session) -> dict[str, Job]:
    jobs = session.scalars(select(Job)).all()
    return {j.source_job_id: j for j in jobs}


def test_seed_has_30_postings_3_duplicates():
    assert len(SEED_JOBS) == 30
    assert sum(1 for j in SEED_JOBS if j.expected == "duplicate") == 3


def test_seed_run_counts(session, evaluator):
    run = _seed(session, evaluator)
    expected = [j for j in SEED_JOBS if j.expected != "duplicate"]
    assert session.scalar(select(func.count()).select_from(Job)) == 27
    assert run.new_count == 27
    assert run.skipped_count == sum(j.expected == "Skipped" for j in expected)
    assert run.shortlisted_count == sum(j.expected == "Shortlisted" for j in expected)
    assert run.errors == [] and run.finished_at is not None and run.site == "seed"


@pytest.mark.parametrize("seed", [j for j in SEED_JOBS if j.expected != "duplicate"], ids=lambda j: j.key)
def test_seed_expected_outcome(session, evaluator, seed):
    _seed(session, evaluator)
    job = _by_key(session)[seed.key]
    assert job.status == seed.expected, job.status_reason
    assert job.match_score is not None and job.status_reason
    if seed.expected_rule:
        prefix = {"senior": "Skipped - Senior Role", "student": "Skipped - Student Position",
                  "experience": "Skipped - Experience"}[seed.expected_rule]
        assert job.status_reason.startswith(prefix), job.status_reason
    if seed.expected_category:
        assert f"[{seed.expected_category}]" in job.status_reason, job.status_reason
    assert job.cv_id == "main"


def test_seed_is_idempotent(session, evaluator):
    _seed(session, evaluator)
    snapshot = {j.id: (j.status, j.match_score) for j in session.scalars(select(Job))}
    run2 = _seed(session, evaluator)
    assert run2.new_count == 0 and run2.skipped_count == 0 and run2.shortlisted_count == 0
    assert session.scalar(select(func.count()).select_from(Job)) == 27
    assert {j.id: (j.status, j.match_score) for j in session.scalars(select(Job))} == snapshot
    assert session.scalar(select(func.count()).select_from(Run)) == 2


def test_seed_duplicates_are_logged(session, evaluator):
    _seed(session, evaluator)
    results = [e.result for e in session.scalars(select(Event).where(Event.action == "job_duplicate"))]
    assert len(results) == 3
    assert any("by fingerprint" in r for r in results)
    assert any("by url" in r for r in results)
    assert any("by source_job_id" in r for r in results)


def test_every_job_has_events(session, evaluator):
    _seed(session, evaluator)
    for job in session.scalars(select(Job)):
        actions = {e.action for e in session.scalars(select(Event).where(Event.job_id == job.id))}
        assert "job_inserted" in actions
        assert actions & {"job_skipped", "job_shortlisted", "job_scored"}
    run_actions = {e.action for e in session.scalars(select(Event).where(Event.job_id.is_(None)))}
    assert {"cv_loaded", "run_started", "run_finished"} <= run_actions


def test_language_detection(session, evaluator):
    _seed(session, evaluator)
    jobs = _by_key(session)
    assert jobs["s01"].language == "en"
    assert jobs["s14"].language in ("he", "mixed")
    assert jobs["s06"].language == "he"


def test_years_and_matched_skills_stored(session, evaluator):
    _seed(session, evaluator)
    jobs = _by_key(session)
    assert jobs["s09"].years_required_min == 3
    assert jobs["s24"].years_required_min == 1  # 5+ years 'advantage' line ignored
    assert jobs["s01"].matched_skills == ["Python", "Django", "PostgreSQL", "Docker"]


def test_process_pending_only_touches_unprocessed(session, evaluator):
    job, _ = ingest(session, JobPosting(source="manual", source_job_id="m1", title="Junior Python Developer",
                                        company="X", location="Y", description_text="Python, Django",
                                        posted_at=NOW - timedelta(days=1)), NOW)
    assert job.match_score is None
    c1 = process_pending(session, evaluator, NOW)
    c2 = process_pending(session, evaluator, NOW)
    assert c1["processed"] == 1 and c2["processed"] == 0
    assert job.status == "Shortlisted"


def test_rescore_respects_user_statuses(session, evaluator, cfg):
    _seed(session, evaluator)
    jobs = _by_key(session)
    jobs["s01"].status = "Applied"           # user-owned status
    jobs["s01"].status_reason = "Applied manually"
    jobs["s18"].cv_id = "main"
    session.commit()

    # stricter threshold -> some Shortlisted become New, but 'Applied' is never touched
    object.__setattr__(cfg.search, "shortlist_threshold", 75)
    counts = rescore(session, evaluator, NOW)
    session.commit()
    assert counts["changed"] > 0
    assert jobs["s01"].status == "Applied" and jobs["s01"].status_reason == "Applied manually"
    assert jobs["s12"].status == "New"       # 72 < 75
    assert jobs["s17"].status == "Shortlisted"  # 90
    assert session.scalar(select(func.count()).select_from(Event).where(Event.action == "job_rescored")) \
        == counts["changed"]


def test_rescore_keeps_user_cv_override(session, evaluator, cfg, matcher):
    from job_hunter.core.cv_loader import CVRecord
    from job_hunter.core.models import CVVersion
    from job_hunter.core.pipeline import Evaluator

    session.add(CVVersion(id="alt", file="alt.pdf", sha256="x", extracted_text="", skills_json=[]))
    _seed(session, evaluator)
    job = _by_key(session)["s17"]
    job.cv_id = "alt"
    session.commit()
    records = evaluator.cv_records + [CVRecord("alt", "alt.pdf", "Alt", ["ai"], False, [], False)]
    rescore(session, Evaluator(cfg, records, matcher), NOW)
    assert job.cv_id == "alt"


def test_bad_posting_does_not_abort_run(session, evaluator):
    good = JobPosting(source="t", source_job_id="1", title="Junior Developer", company="A", location="B")
    bad = JobPosting(source="t", source_job_id="2", title=None, company="C", location="D")  # NOT NULL title
    run = run_postings(session, evaluator, [bad, good], site="t", now=NOW)
    session.commit()
    assert run.new_count == 1 and len(run.errors) == 1
    assert session.scalar(select(func.count()).select_from(Job)) == 1
