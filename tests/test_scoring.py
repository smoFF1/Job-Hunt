from __future__ import annotations

from datetime import timedelta

import pytest

from job_hunter.core.categories import Categorizer
from job_hunter.core.scoring import Scorer
from tests.conftest import CORE_SKILLS, NOW


@pytest.fixture
def scorer(cfg, matcher) -> Scorer:
    return Scorer(cfg.search, matcher)


@pytest.fixture
def cat(cfg) -> Categorizer:
    return Categorizer(cfg.search)


def test_role_points(scorer, cat):
    assert scorer.role_points("Backend Developer", cat.categorize("Backend Developer", ""))[0] == 30
    assert scorer.role_points("Junior Software Engineer",
                              cat.categorize("Junior Software Engineer", ""))[0] == 20
    assert scorer.role_points("מפתח/ת Java", cat.categorize("מפתח/ת Java", ""))[0] == 20
    assert scorer.role_points("Team Member", cat.categorize("Team Member", "backend services"))[0] == 15
    assert scorer.role_points("Sales Representative", cat.categorize("Sales Representative", ""))[0] == 0


def test_skill_points(scorer):
    cand = set(CORE_SKILLS)
    assert scorer.skill_points([], cand)[0] == 10
    assert scorer.skill_points(["Python", "Django"], cand)[0] == 30
    assert scorer.skill_points(["Python", "Go"], cand)[0] == 15
    assert scorer.skill_points(["Go", "AWS", "Kubernetes"], cand)[0] == 0
    pts, matched, _ = scorer.skill_points(["Python", "Go", "Rust"], cand)
    assert pts == 10 and matched == ["Python"]


@pytest.mark.parametrize("text,years,pts", [
    ("Junior developer", None, 20),
    ("ג'וניור", None, 20),
    ("מתאים לבוגרי תואר", None, 20),
    ("Entry-level position", None, 20),
    ("ללא ניסיון", None, 20),
    ("Developer", 1, 20),
    ("Developer", 0, 20),
    ("Developer", None, 10),
    ("Developer", 1.5, 10),
])
def test_seniority_points(scorer, text, years, pts):
    assert scorer.seniority_points(text, years)[0] == pts


def test_domain_points(scorer):
    assert scorer.domain_points("Software for defense systems")[0] == 10
    assert scorer.domain_points("חברה ביטחונית")[0] == 10
    assert scorer.domain_points("Iron Shield Cyber")[0] == 10
    assert scorer.domain_points("E-commerce website")[0] == 0


@pytest.mark.parametrize("days,pts", [(0, 10), (3, 10), (4, 7), (7, 7), (8, 4), (14, 4), (15, 0), (60, 0)])
def test_recency_points(scorer, days, pts):
    assert scorer.recency_points(NOW - timedelta(days=days), NOW)[0] == pts


def test_recency_unknown(scorer):
    assert scorer.recency_points(None, NOW)[0] == 5


def test_max_score_is_100(scorer, cat):
    title = "Junior Embedded Developer"
    desc = "C++ and Linux for defense systems."
    b, job_skills, matched = scorer.score(title=title, description=desc, company="", posted_at=NOW,
                                          category=cat.categorize(title, desc), years_min=None,
                                          candidate_skills=set(CORE_SKILLS), now=NOW)
    assert (b.role, b.skills, b.seniority, b.domain, b.recency) == (30, 30, 20, 10, 10)
    assert b.total == 100 and matched == ["C++", "Linux"]


# --- threshold boundary through the full evaluator ------------------------------------------

def _eval(evaluator, title, desc, days):
    return evaluator.evaluate(title=title, description=desc, posted_at=NOW - timedelta(days=days), now=NOW)


def test_threshold_boundary_50_shortlisted(evaluator):
    # role 30 (DevOps title) + skills 0 (0/1) + seniority 10 + domain 0 + recency 10 = 50
    ev = _eval(evaluator, "DevOps Engineer", "Kubernetes clusters.", 1)
    assert ev.score == 50 and ev.status == "Shortlisted"
    assert ev.status_reason.startswith("Shortlisted - Score 50 >= 50 [DevOps]")


def test_threshold_boundary_49_new(evaluator):
    # role 30 + skills 0 + seniority 10 + domain 0 + recency 7 = 47 -> New
    ev = _eval(evaluator, "DevOps Engineer", "Kubernetes clusters.", 5)
    assert ev.score == 47 and ev.status == "New"
    assert ev.status_reason.startswith("New - Score 47 below threshold 50")


def test_skipped_jobs_still_get_a_score(evaluator):
    ev = _eval(evaluator, "Senior Backend Engineer", "Python, Django", 1)
    assert ev.status == "Skipped" and ev.score > 0
    assert ev.status_reason.startswith("Skipped - Senior Role")


def test_threshold_is_configurable(tmp_path):
    from job_hunter.config import load_config
    from job_hunter.core.pipeline import Evaluator
    from tests.conftest import write_config

    search = {"thresholds": {"experience_cutoff": 3, "shortlist_threshold": 40}}
    cfg = load_config(write_config(tmp_path / "c", search_overrides=search), tmp_path)
    ev = Evaluator(cfg, []).evaluate(title="DevOps Engineer", description="2+ years of experience, k8s",
                                     posted_at=NOW - timedelta(days=5), now=NOW)
    assert ev.status == "Shortlisted" and ev.score == 47  # 2 years passes with cutoff 3


# --- student-in-description penalty ----------------------------------------------------------

@pytest.mark.parametrize("title,desc,pts", [
    ("Backend Developer", "Position suitable for a CS student in 3rd year.", -20),
    ("מפתח/ת Backend", "המשרה מיועדת לסטודנטים ביתרת לימודים של שנה.", -20),
    ("Software Engineer", "משרת סטודנט/ית בצוות הפיתוח.", -20),
    ("Backend Developer", "Open to final-year students or fresh graduates.", 0),
    ("מפתח/ת תוכנה", "מתאים לסטודנטים או לבוגרים טריים.", 0),
    ("Graduate Software Engineer", "Final-year students may also apply.", 0),
    ("בוגר/ת מדעי המחשב", "גם סטודנטים בשנה ג׳ יכולים להגיש.", 0),
    ("Backend Developer", "Python and PostgreSQL.", 0),
])
def test_student_description_penalty_points(scorer, title, desc, pts):
    got, note = scorer.penalty_points(title, desc)
    assert got == pts
    assert ("student keyword in description" in note) is (pts < 0)


def test_student_in_description_penalizes_without_skipping(evaluator):
    # Without student in description: role 30 + skills 0 (Go) + seniority 10 + domain 0 + recency 10 = 50 (Shortlisted)
    # With student in description (no graduate): 50 - 20 = 30 -> New (NOT Skipped; title-only skip stays as is)
    ev = _eval(evaluator, "Backend Developer", "Part-time role for a CS student. Go services.", 1)
    assert ev.status == "New"
    assert not ev.decision.skip
    assert ev.breakdown.penalty == -20
    assert ev.score == 30
    assert "penalty -20" in ev.status_reason


def test_student_title_still_skips(evaluator):
    ev = _eval(evaluator, "Student Backend Developer", "Python and Django.", 1)
    assert ev.status == "Skipped" and ev.decision.rule == "student"


def test_student_penalty_clamps_at_zero(evaluator):
    # role 0 + skills 0 (Go) + seniority 10 + domain 0 + recency 0 (30d) - 20 = -10 -> clamped to 0
    ev = _eval(evaluator, "Office Assistant", "For a student knowing Go.", 30)
    assert ev.score == 0 and ev.breakdown.penalty == -20


def test_student_penalty_is_configurable(tmp_path):
    from job_hunter.config import load_config
    from job_hunter.core.pipeline import Evaluator
    from tests.conftest import write_config

    cfg_35 = load_config(write_config(tmp_path / "c1",
                                      search_overrides={"scoring": {"penalties": {"student_in_description": -35}}}),
                         tmp_path)
    ev_35 = Evaluator(cfg_35, []).evaluate(title="Backend Developer",
                                           description="For a student. Python.", posted_at=NOW, now=NOW)
    assert ev_35.breakdown.penalty == -35 and ev_35.score == 45  # 30+30+10+0+10 - 35 = 45 -> New
    assert ev_35.status == "New"

    cfg_0 = load_config(write_config(tmp_path / "c2",
                                     search_overrides={"scoring": {"penalties": {"student_in_description": 0}}}),
                        tmp_path)
    ev_0 = Evaluator(cfg_0, []).evaluate(title="Backend Developer",
                                         description="For a student. Python.", posted_at=NOW, now=NOW)
    assert ev_0.breakdown.penalty == 0 and ev_0.score == 80 and ev_0.status == "Shortlisted"
    assert "penalty" not in ev_0.status_reason
