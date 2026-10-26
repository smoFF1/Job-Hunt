"""Shared fixtures. Tests never depend on the real (gitignored) profile.yaml or CV."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from job_hunter import DEFAULT_CONFIG_DIR
from job_hunter.config import AppConfig, load_config
from job_hunter.core.cv_loader import sync_cvs
from job_hunter.core.db import get_engine, init_db, make_session_factory
from job_hunter.core.pipeline import Evaluator
from job_hunter.core.skills import SkillMatcher

NOW = datetime(2026, 10, 26, 9, 0, 0)

CORE_SKILLS = ["Python", "Java", "C++", "C#", "JavaScript", "React", "React Native", "Node.js", "FastAPI",
               "Django", "Docker", "Linux", "Git", "PostgreSQL", "MySQL", "REST", "LLM/GenAI"]

# Fake CV text: long enough to pass the quality check; only contains core skills.
FAKE_CV_TEXT = ("Jane Doe — Software Engineer. B.Sc. Computer Science. Projects built with Python, "
                "Django and PostgreSQL; a React Native app; services in Node.js with Docker on Linux. "
                "Coursework in Java, C++ and C#. Version control with Git. " * 2)


def write_config(dst: Path, *, cvs: list[dict] | None = None, profile_overrides: dict | None = None,
                 search_overrides: dict | None = None) -> Path:
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy(DEFAULT_CONFIG_DIR / "skills.yaml", dst / "skills.yaml")
    search = yaml.safe_load((DEFAULT_CONFIG_DIR / "search.yaml").read_text(encoding="utf-8"))
    for k, v in (search_overrides or {}).items():
        search[k] = v
    (dst / "search.yaml").write_text(yaml.safe_dump(search, allow_unicode=True), encoding="utf-8")
    profile = {"first_name": "Jane", "last_name": "Doe", "email": "jane@example.com",
               "level": "entry-level", "core_skills": CORE_SKILLS}
    profile.update(profile_overrides or {})
    (dst / "profile.yaml").write_text(yaml.safe_dump(profile, allow_unicode=True), encoding="utf-8")
    if cvs is None:
        cvs = [{"id": "main", "file": "assets/cv/test_cv.pdf", "label": "General",
                "tags": ["general", "fullstack", "backend", "ai"], "default": True}]
    (dst / "cvs.yaml").write_text(yaml.safe_dump({"cvs": cvs}), encoding="utf-8")
    return dst


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A temp project root with config/ and a dummy CV file."""
    write_config(tmp_path / "config")
    cv = tmp_path / "assets" / "cv" / "test_cv.pdf"
    cv.parent.mkdir(parents=True)
    cv.write_bytes(b"%PDF-1.4 fake content v1")
    return tmp_path


@pytest.fixture
def cfg(project: Path) -> AppConfig:
    return load_config(project / "config", project)


@pytest.fixture
def factory(tmp_path: Path):
    engine = get_engine(tmp_path / "test.db")
    init_db(engine)
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture
def session(factory):
    s = factory()
    yield s
    s.close()


@pytest.fixture
def matcher(cfg: AppConfig) -> SkillMatcher:
    return SkillMatcher(cfg.skills)


def fake_extractor(_path: Path) -> str:
    return FAKE_CV_TEXT


@pytest.fixture
def evaluator(cfg, session, matcher) -> Evaluator:
    records = sync_cvs(session, cfg, matcher, extractor=fake_extractor)
    session.commit()
    return Evaluator(cfg, records, matcher)
