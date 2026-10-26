from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from job_hunter import DEFAULT_CONFIG_DIR, PROJECT_ROOT
from job_hunter.config import load_config, load_skills
from job_hunter.core.cv_loader import CVRecord, default_cv, extract_pdf_text, suggest_cv, sync_cvs
from job_hunter.core.models import CVVersion, Event
from job_hunter.core.skills import SkillMatcher
from tests.conftest import FAKE_CV_TEXT, write_config


class CountingExtractor:
    def __init__(self, text: str = FAKE_CV_TEXT):
        self.calls = 0
        self.text = text

    def __call__(self, _path: Path) -> str:
        self.calls += 1
        return self.text


def _actions(session) -> list[str]:
    return [e.action for e in session.scalars(select(Event).order_by(Event.id)).all()]


def test_first_load_extracts_and_stores(session, cfg, matcher):
    ex = CountingExtractor()
    [rec] = sync_cvs(session, cfg, matcher, extractor=ex)
    assert ex.calls == 1 and rec.ok and rec.default
    assert {"Python", "Django", "PostgreSQL", "React Native", "Node.js", "Docker"} <= set(rec.skills)
    row = session.get(CVVersion, "main")
    assert row.sha256 and row.skills_json == rec.skills and row.is_default
    assert _actions(session) == ["cv_loaded"]


def test_unchanged_file_is_cache_hit(session, cfg, matcher):
    ex = CountingExtractor()
    sync_cvs(session, cfg, matcher, extractor=ex)
    [rec] = sync_cvs(session, cfg, matcher, extractor=ex)
    assert ex.calls == 1 and rec.ok
    assert _actions(session) == ["cv_loaded", "cv_cache_hit"]


def test_changed_file_is_reextracted(session, cfg, matcher, project):
    ex = CountingExtractor()
    sync_cvs(session, cfg, matcher, extractor=ex)
    old_sha = session.get(CVVersion, "main").sha256
    (project / "assets" / "cv" / "test_cv.pdf").write_bytes(b"%PDF-1.4 fake content v2")
    ex.text = FAKE_CV_TEXT + " Also Kubernetes and AWS."
    [rec] = sync_cvs(session, cfg, matcher, extractor=ex)
    assert ex.calls == 2
    assert session.get(CVVersion, "main").sha256 != old_sha
    assert "Kubernetes" in rec.skills
    assert _actions(session)[-1] == "cv_reextracted"


@pytest.mark.parametrize("text,problem", [("", "too short"), ("   \n ", "too short"),
                                          ("lorem ipsum " * 40, "no known skills")])
def test_bad_extraction_falls_back_without_crash(session, cfg, matcher, text, problem):
    [rec] = sync_cvs(session, cfg, matcher, extractor=CountingExtractor(text))
    assert not rec.ok and problem in rec.problem
    assert "cv_extract_failed" in _actions(session)


def test_extractor_exception_does_not_crash(session, cfg, matcher):
    def boom(_p):
        raise RuntimeError("corrupt pdf")

    warnings: list[str] = []
    [rec] = sync_cvs(session, cfg, matcher, extractor=boom, warnings=warnings)
    assert not rec.ok and warnings and "falling back" in warnings[0]


def test_missing_file_falls_back(session, tmp_path, matcher):
    cvs = [{"id": "gone", "file": "assets/cv/missing.pdf", "tags": ["ai"], "default": True}]
    cfg = load_config(write_config(tmp_path / "config", cvs=cvs), tmp_path)
    [rec] = sync_cvs(session, cfg, matcher, extractor=CountingExtractor())
    assert not rec.ok and rec.problem == "file not found"
    assert session.get(CVVersion, "gone") is not None  # row exists so jobs.cv_id FK stays valid


def test_real_pdf_parser_on_garbage_returns_empty(tmp_path):
    p = tmp_path / "bad.pdf"
    p.write_bytes(b"not a pdf at all")
    assert extract_pdf_text(p).strip() == ""


def test_fallback_candidate_skills_are_core_only(session, cfg, matcher):
    from job_hunter.core.pipeline import Evaluator

    records = sync_cvs(session, cfg, matcher, extractor=CountingExtractor(""))
    ev = Evaluator(cfg, records, matcher)
    assert ev.candidate_skills(records[0]) == set(ev.core_skills)


def _rec(id_, tags, default=False):
    return CVRecord(id=id_, file=f"{id_}.pdf", label=id_, tags=tags, default=default, ok=True)


def test_suggest_cv():
    recs = [_rec("main", ["general", "fullstack"], True), _rec("ai", ["ai"]), _rec("emb", ["embedded"])]
    assert suggest_cv("ai", recs).id == "ai"
    assert suggest_cv("embedded", recs).id == "emb"
    assert suggest_cv("fullstack", recs).id == "main"
    assert suggest_cv("devops", recs).id == "main"      # no tag match -> default
    assert suggest_cv("general", recs).id == "main"
    assert suggest_cv("ai", []) is None
    assert default_cv(recs).id == "main"


def test_suggest_cv_prefers_default_on_tie():
    recs = [_rec("a", ["ai"]), _rec("b", ["ai"], True)]
    assert suggest_cv("ai", recs).id == "b"


REAL_CVS = sorted((PROJECT_ROOT / "assets" / "cv").glob("*.pdf"))


@pytest.mark.skipif(not REAL_CVS, reason="personal CV not present in assets/cv/ (gitignored)")
def test_real_cv_extracts():
    skills = load_skills(DEFAULT_CONFIG_DIR / "skills.yaml")
    text = extract_pdf_text(REAL_CVS[0])
    found = SkillMatcher(skills).extract(text)
    assert len(text.strip()) > 200
    assert {"Python", "Java"} <= set(found)
