from __future__ import annotations

import pytest

from job_hunter import DEFAULT_CONFIG_DIR
from job_hunter.config import ConfigError, load_config, load_cvs, load_profile
from tests.conftest import write_config


def test_load_repo_search_and_skills(cfg):
    s = cfg.search
    assert s.experience_cutoff == 2
    assert s.shortlist_threshold == 50
    assert s.llm_tiebreaker is False
    assert [c.name for c in s.categories] == [
        "QA Automation", "AI", "Fullstack", "Backend", "Frontend", "DevOps", "Embedded/RT", "Defense/Security"]
    assert s.scoring.role_title_category + s.scoring.skills_max + s.scoring.seniority_boost \
        + s.scoring.domain_bonus + max(p for _, p in s.scoring.recency_buckets) == 100
    assert s.scoring.student_description_penalty == -20
    assert s.sites["linkedin"]["enabled"] is False


def test_all_core_skills_are_in_vocabulary(cfg):
    assert cfg.warnings == []


def test_example_profile_is_valid_and_fake():
    example_path = DEFAULT_CONFIG_DIR / "profile.example.yaml"
    prof = load_profile(example_path)
    assert (prof.first_name, prof.last_name, prof.email) == ("Jane", "Doe", "jane.doe@example.com")
    real_path = DEFAULT_CONFIG_DIR / "profile.yaml"
    if real_path.exists():
        real = load_profile(real_path)
        raw = example_path.read_text(encoding="utf-8")
        assert real.last_name not in raw and real.email not in raw


def test_example_cvs_is_valid_and_placeholder():
    warnings: list[str] = []
    entries = load_cvs(DEFAULT_CONFIG_DIR / "cvs.example.yaml", warnings)
    assert warnings == []
    assert len(entries) == 1
    assert (entries[0].id, entries[0].file, entries[0].label, entries[0].default) == (
        "main", "assets/cv/my_cv.pdf", "General", True)
    assert entries[0].tags == ["general", "fullstack", "backend", "ai"]
    real_path = DEFAULT_CONFIG_DIR / "cvs.yaml"
    if real_path.exists():
        real = load_cvs(real_path, [])
        assert len(real) >= 1 and real[0].file != entries[0].file


def test_missing_profile_gives_hint(tmp_path):
    d = write_config(tmp_path / "config")
    (d / "profile.yaml").unlink()
    with pytest.raises(ConfigError, match="profile.example.yaml"):
        load_config(d, tmp_path)


def test_missing_cvs_gives_hint(tmp_path):
    d = write_config(tmp_path / "config")
    (d / "cvs.yaml").unlink()
    with pytest.raises(ConfigError, match="cvs.example.yaml"):
        load_config(d, tmp_path)


def test_positive_student_penalty_is_an_error(tmp_path):
    d = write_config(tmp_path / "config",
                     search_overrides={"scoring": {"penalties": {"student_in_description": 10}}})
    with pytest.raises(ConfigError, match="student_in_description"):
        load_config(d, tmp_path)


def test_profile_requires_email(tmp_path):
    d = write_config(tmp_path / "config", profile_overrides={"email": ""})
    with pytest.raises(ConfigError, match="email"):
        load_config(d, tmp_path)


def test_unknown_core_skill_warns(tmp_path):
    d = write_config(tmp_path / "config", profile_overrides={"core_skills": ["Python", "Cobol"]})
    cfg = load_config(d, tmp_path)
    assert any("Cobol" in w for w in cfg.warnings)


def test_two_default_cvs_is_an_error(tmp_path):
    cvs = [{"id": "a", "file": "a.pdf", "default": True}, {"id": "b", "file": "b.pdf", "default": True}]
    d = write_config(tmp_path / "config", cvs=cvs)
    with pytest.raises(ConfigError, match="more than one default"):
        load_config(d, tmp_path)


def test_no_default_cv_picks_first_with_warning(tmp_path):
    cvs = [{"id": "a", "file": "a.pdf"}, {"id": "b", "file": "b.pdf"}]
    cfg = load_config(write_config(tmp_path / "config", cvs=cvs), tmp_path)
    assert cfg.cvs[0].default is True and cfg.cvs[1].default is False
    assert any("no default CV" in w for w in cfg.warnings)


def test_duplicate_cv_id_is_an_error(tmp_path):
    cvs = [{"id": "a", "file": "a.pdf", "default": True}, {"id": "a", "file": "b.pdf"}]
    with pytest.raises(ConfigError, match="duplicate id"):
        load_config(write_config(tmp_path / "config", cvs=cvs), tmp_path)


def test_site_without_enabled_flag_is_an_error(tmp_path):
    d = write_config(tmp_path / "config", search_overrides={"sites": {"alljobs": {"pages": 2}}})
    with pytest.raises(ConfigError, match="enabled"):
        load_config(d, tmp_path)


def test_invalid_yaml(tmp_path):
    d = write_config(tmp_path / "config")
    (d / "search.yaml").write_text("thresholds: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="Invalid YAML"):
        load_config(d, tmp_path)
