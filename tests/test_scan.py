"""Phase 4 offline tests: on-demand scan UI, subprocess execution, single-scan lock,
stale-lock recovery, cooldown (including dry-run), block lockout (240 min), multi-site
fault isolation, stop button, CSRF/Origin/input validation, NEW badge & filter, and
SQLite WAL concurrent reads during writes. Zero live network requests.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

import job_hunter.scrapers.registry as registry_mod
from job_hunter.cli import main as cli_main
from job_hunter.config import load_config
from job_hunter.core.db import get_engine, init_db, make_session_factory
from job_hunter.core.models import CVVersion, Event, Job, Run, utcnow
from job_hunter.dashboard.app import create_app
from job_hunter.scrapers.scan import (
    ScanAlreadyRunningError,
    ScanCooldownError,
    acquire_scan_lock,
    get_site_scan_statuses,
    read_scan_state,
    run_scan,
    scan_lock_path,
    write_scan_state,
)
from tests._mock_scrapers import register_mock_scrapers
from tests.conftest import fake_extractor, write_config


@pytest.fixture
def mock_registry(monkeypatch):
    """Register `mock_alpha`, `mock_beta`, `mock_gamma` in parent + child subprocesses, restoring after test."""
    saved = dict(registry_mod._REGISTRY)
    monkeypatch.setenv("JOB_HUNTER_EXTRA_SCRAPERS", "tests._mock_scrapers")
    register_mock_scrapers()
    try:
        yield
    finally:
        registry_mod._REGISTRY.clear()
        registry_mod._REGISTRY.update(saved)


@pytest.fixture
def patch_cv_extractor(monkeypatch):
    import job_hunter.core.cv_loader as cvl

    monkeypatch.setattr(cvl, "extract_pdf_text", fake_extractor)
    monkeypatch.setattr(cvl.sync_cvs, "__defaults__", (fake_extractor, None))


def _make_scan_env(
    tmp_path: Path,
    *,
    roles: list[str] | None = None,
    sites: dict[str, dict[str, Any]] | None = None,
):
    if sites is None:
        sites = {
            "alljobs": {"enabled": False},
            "mock_alpha": {
                "enabled": True,
                "min_minutes_between_scans": 30,
                "min_minutes_after_block": 240,
                "max_pages_per_query": 2,
                "max_pages_per_run": 20,
            },
            "mock_beta": {
                "enabled": True,
                "min_minutes_between_scans": 30,
                "min_minutes_after_block": 240,
                "max_pages_per_query": 2,
                "max_pages_per_run": 20,
            },
            "mock_gamma": {
                "enabled": False,
                "min_minutes_between_scans": 30,
            },
        }
    overrides: dict[str, Any] = {"sites": sites}
    if roles is not None:
        overrides["roles"] = roles
    else:
        overrides["roles"] = ["Junior Python Developer", "Backend Engineer"]

    cfg_dir = write_config(tmp_path / "custom_cfg", cvs=[], search_overrides=overrides)
    cfg = load_config(cfg_dir, tmp_path)
    db_path = tmp_path / "custom_data" / "scan_test.db"
    engine = get_engine(db_path)
    init_db(engine)
    factory = make_session_factory(engine)
    return db_path, cfg_dir, cfg, engine, factory


def _get_csrf(client: TestClient) -> str:
    resp = client.get("/")
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")
    inp = soup.select_one("#scan-start-form input[name='csrf_token']")
    assert inp is not None and inp.get("value")
    return str(inp["value"])


def _wait_for_scan_completion(client: TestClient, *, timeout: float = 15.0) -> str:
    deadline = time.monotonic() + timeout
    last_html = ""
    while time.monotonic() < deadline:
        r = client.get("/scan/status", headers={"HX-Request": "true"})
        assert r.status_code == 200
        last_html = r.text
        if 'hx-trigger="every 2s"' not in last_html:
            return last_html
        time.sleep(0.08)
    raise AssertionError(f"Scan did not finish within {timeout}s; last HTML:\n{last_html}")


# --------------------------------------------------------------------------- 1. Registry-driven site list


def test_site_list_built_automatically_from_registry_and_config(tmp_path, mock_registry):
    db_path, cfg_dir, cfg, engine, factory = _make_scan_env(tmp_path)
    try:
        app = create_app(db_path=db_path, session_factory=factory, cfg=cfg, config_dir=cfg_dir, root=tmp_path)
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            resp = client.get("/")
            assert resp.status_code == 200
            soup = BeautifulSoup(resp.text, "html.parser")

            checkboxes = {
                cb["value"]: cb
                for cb in soup.select("#scan-start-form input[type='checkbox'][name='sites']")
            }
            # All registered scrapers appear; unregistered `linkedin` does not
            assert set(checkboxes.keys()) == {"alljobs", "mock_alpha", "mock_beta", "mock_gamma"}
            assert "linkedin" not in checkboxes

            # Enabled sites checked by default; disabled sites unchecked & disabled
            assert checkboxes["mock_alpha"].has_attr("checked") and not checkboxes["mock_alpha"].has_attr("disabled")
            assert checkboxes["mock_beta"].has_attr("checked") and not checkboxes["mock_beta"].has_attr("disabled")
            assert not checkboxes["mock_gamma"].has_attr("checked") and checkboxes["mock_gamma"].has_attr("disabled")
            assert not checkboxes["alljobs"].has_attr("checked") and checkboxes["alljobs"].has_attr("disabled")
            assert "disabled in config" in resp.text
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- 2. Extra query vs all roles


def test_extra_query_runs_only_one_off_query_and_blank_runs_all_roles(tmp_path, mock_registry):
    rec_file = tmp_path / "recorded_queries.json"
    sites = {
        "alljobs": {"enabled": False},
        "mock_alpha": {
            "enabled": True,
            "min_minutes_between_scans": 0,
            "max_pages_per_query": 2,
            "max_pages_per_run": 40,
            "mock_pages_per_query": 5,  # should be capped to max_pages_per_query=2!
            "mock_record_file": str(rec_file),
        },
        "mock_beta": {"enabled": False},
        "mock_gamma": {"enabled": False},
    }
    db_path, cfg_dir, cfg, engine, factory = _make_scan_env(
        tmp_path,
        roles=["Role One", "Role Two", "Role Three"],
        sites=sites,
    )
    try:
        app = create_app(db_path=db_path, session_factory=factory, cfg=cfg, config_dir=cfg_dir, root=tmp_path)
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            csrf = _get_csrf(client)

            # A. Extra query filled -> runs ONLY that one-off query, respecting max_pages_per_query=2
            r1 = client.post(
                "/scan",
                data={"sites": ["mock_alpha"], "extra_query": "Rust Embedded", "csrf_token": csrf},
                headers={"HX-Request": "true"},
            )
            assert r1.status_code == 200
            _wait_for_scan_completion(client)

            recorded_1 = json.loads(rec_file.read_text(encoding="utf-8"))
            assert recorded_1 == ["Rust Embedded"]
            st1 = read_scan_state(db_path)
            assert st1["pages_fetched"] == 2  # capped by max_pages_per_query=2

            # B. Extra query blank -> runs all 3 configured roles (3 * 2 = 6 pages)
            rec_file.unlink()
            r2 = client.post(
                "/scan",
                data={"sites": ["mock_alpha"], "extra_query": "   ", "csrf_token": csrf},
                headers={"HX-Request": "true"},
            )
            assert r2.status_code == 200
            _wait_for_scan_completion(client)

            recorded_2 = json.loads(rec_file.read_text(encoding="utf-8"))
            assert recorded_2 == ["Role One", "Role Two", "Role Three"]
            st2 = read_scan_state(db_path)
            assert st2["pages_fetched"] == 6
    finally:
        engine.dispose()


# ------------------------------------------------------------------ 3. Dry-run saves nothing + triggers cooldown


def test_dry_run_saves_nothing_and_subsequent_scan_within_cooldown_is_rejected(tmp_path, mock_registry):
    sites = {
        "alljobs": {"enabled": False},
        "mock_alpha": {
            "enabled": True,
            "min_minutes_between_scans": 30,
            "min_minutes_after_block": 240,
        },
        "mock_beta": {"enabled": False},
        "mock_gamma": {"enabled": False},
    }
    db_path, cfg_dir, cfg, engine, factory = _make_scan_env(tmp_path, sites=sites)
    try:
        app = create_app(db_path=db_path, session_factory=factory, cfg=cfg, config_dir=cfg_dir, root=tmp_path)
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            csrf = _get_csrf(client)

            # 1. Trigger a DRY RUN from dashboard
            r_dry = client.post(
                "/scan",
                data={"sites": ["mock_alpha"], "dry_run": "1", "csrf_token": csrf},
                headers={"HX-Request": "true"},
            )
            assert r_dry.status_code == 200
            done_html = _wait_for_scan_completion(client)
            assert "Dry run preview" in done_html
            assert "Junior Software Developer (mock_alpha)" in done_html

            # Verify ZERO DB writes across all tables
            with factory() as s:
                assert s.scalar(select(func.count()).select_from(Job)) == 0
                assert s.scalar(select(func.count()).select_from(Run)) == 0
                assert s.scalar(select(func.count()).select_from(Event)) == 0
                assert s.scalar(select(func.count()).select_from(CVVersion)) == 0

            # Verify last_request_at was recorded in state file despite dry-run
            state = read_scan_state(db_path)
            assert state["dry_run"] is True
            assert state["site_history"]["mock_alpha"]["last_request_at"] is not None

            # Verify UI now shows "available in N min" and disables the checkbox + Scan button
            page = client.get("/")
            soup = BeautifulSoup(page.text, "html.parser")
            assert "available in " in page.text and " min" in page.text
            cb = soup.select_one("#scan-start-form input[name='sites'][value='mock_alpha']")
            assert cb is not None and cb.has_attr("disabled")
            scan_btn = soup.find("button", attrs={"form": "scan-start-form"})
            assert scan_btn is not None and scan_btn.has_attr("disabled")

            # 2. Second scan (both normal AND dry-run) within cooldown is REJECTED (HTTP 429 & CLI exit 1)
            r_reject_normal = client.post(
                "/scan",
                data={"sites": ["mock_alpha"], "csrf_token": csrf},
                headers={"HX-Request": "true"},
            )
            assert r_reject_normal.status_code == 429
            assert "available in" in r_reject_normal.text

            r_reject_dry = client.post(
                "/scan",
                data={"sites": ["mock_alpha"], "dry_run": "1", "csrf_token": csrf},
                headers={"HX-Request": "true"},
            )
            assert r_reject_dry.status_code == 429

            cli_code = cli_main([
                f"--db={db_path}",
                f"--config-dir={cfg_dir}",
                f"--root={tmp_path}",
                "scan",
                "--sites=mock_alpha",
            ])
            assert cli_code == 1
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- 4. Fault isolation + 240-min block lockout


def test_one_site_block_does_not_stop_others_and_applies_longer_block_lockout(tmp_path, mock_registry):
    sites = {
        "alljobs": {"enabled": False},
        "mock_alpha": {
            "enabled": True,
            "mock_mode": "captcha",
            "min_minutes_between_scans": 30,
            "min_minutes_after_block": 240,
        },
        "mock_beta": {
            "enabled": True,
            "mock_mode": "ok",
            "min_minutes_between_scans": 30,
            "min_minutes_after_block": 240,
        },
        "mock_gamma": {"enabled": False},
    }
    db_path, cfg_dir, cfg, engine, factory = _make_scan_env(tmp_path, sites=sites)
    try:
        app = create_app(db_path=db_path, session_factory=factory, cfg=cfg, config_dir=cfg_dir, root=tmp_path)
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            csrf = _get_csrf(client)
            r = client.post(
                "/scan",
                data={"sites": ["mock_alpha", "mock_beta"], "csrf_token": csrf},
                headers={"HX-Request": "true"},
            )
            assert r.status_code == 200
            done_html = _wait_for_scan_completion(client)

            # mock_alpha blocked, mock_beta completed and linked its new job!
            assert "CAPTCHA/bot-challenge widget detected" in done_html
            assert "Junior Software Developer (mock_beta)" in done_html

            with factory() as s:
                jobs = list(s.scalars(select(Job)).all())
                assert len(jobs) == 1
                assert jobs[0].source == "mock_beta"
                assert f'/jobs/{jobs[0].id}' in done_html

                runs = list(s.scalars(select(Run).order_by(Run.id)).all())
                assert len(runs) == 2
                assert runs[0].site == "mock_alpha" and len(runs[0].errors) == 1 and runs[0].new_count == 0
                assert runs[1].site == "mock_beta" and runs[1].errors == [] and runs[1].new_count == 1

                ev_actions = {e.action for e in s.scalars(select(Event)).all()}
                assert "scrape_blocked" in ev_actions
                assert "scan_finished" in ev_actions

            # UI shows "blocked — available in N min" + block reason on mock_alpha
            dash = client.get("/")
            assert "blocked — available in " in dash.text
            assert "CAPTCHA/bot-challenge widget detected" in dash.text

            # Fast-forward 60 minutes: normal 30-min cooldown on mock_beta has EXPIRED,
            # while 240-min block lockout on mock_alpha is STILL active (~180 min left)!
            future_now = utcnow() + timedelta(minutes=60)
            with factory() as s:
                statuses = {
                    st.name: st
                    for st in get_site_scan_statuses(s, cfg, db_path, now=future_now)
                }
            assert statuses["mock_beta"].on_cooldown is False
            assert statuses["mock_beta"].selectable is True

            assert statuses["mock_alpha"].blocked_lockout is True
            assert statuses["mock_alpha"].on_cooldown is True
            assert statuses["mock_alpha"].selectable is False
            assert 175 <= statuses["mock_alpha"].cooldown_minutes <= 181
            assert statuses["mock_alpha"].cooldown_label.startswith("blocked — available in ")
            assert "CAPTCHA" in (statuses["mock_alpha"].block_reason or "")

            # Attempting to scan mock_alpha at +60 min is still rejected by ScanCooldownError
            from job_hunter.core.skills import SkillMatcher

            with pytest.raises(ScanCooldownError, match="blocked — available in"):
                run_scan(
                    factory,
                    cfg,
                    SkillMatcher(cfg.skills),
                    db_path=db_path,
                    sites=["mock_alpha"],
                    now=future_now,
                )
    finally:
        engine.dispose()


# ------------------------------------------------------------------ 5. Lock, concurrent rejection & stale recovery


def test_single_scan_lock_rejects_second_click_and_cleans_stale_dead_pid(tmp_path, mock_registry):
    sites = {
        "alljobs": {"enabled": False},
        "mock_alpha": {
            "enabled": True,
            "min_minutes_between_scans": 0,
            "mock_delay_seconds": 1.2,
        },
        "mock_beta": {"enabled": False},
        "mock_gamma": {"enabled": False},
    }
    db_path, cfg_dir, cfg, engine, factory = _make_scan_env(tmp_path, roles=["Slow Query"], sites=sites)
    try:
        # Part A: Seed a STALE running Run + dead-PID lock + stale state BEFORE dashboard starts
        stale_start = utcnow() - timedelta(minutes=15)
        with factory() as s:
            orphan = Run(site="mock_alpha", started_at=stale_start, finished_at=None, errors=[])
            s.add(orphan)
            s.commit()
            orphan_id = orphan.id

        # Spawn and immediately exit a dummy process to obtain a guaranteed dead PID
        dead_proc = subprocess.Popen([sys.executable, "-c", "pass"])
        dead_proc.wait(timeout=5)
        dead_pid = dead_proc.pid

        acquire_scan_lock(db_path, pid=dead_pid, scan_id="stale123", now=stale_start)
        stale_state = read_scan_state(db_path)
        stale_state.update({"scan_id": "stale123", "state": "running", "pid": dead_pid})
        write_scan_state(db_path, stale_state)

        # Starting `create_app` must automatically clean up stale lock, state, and orphaned `Run`!
        app = create_app(db_path=db_path, session_factory=factory, cfg=cfg, config_dir=cfg_dir, root=tmp_path)
        assert not scan_lock_path(db_path).exists()
        assert read_scan_state(db_path)["state"] == "failed"
        with factory() as s:
            cleaned_run = s.get(Run, orphan_id)
            assert cleaned_run is not None and cleaned_run.finished_at is not None
            assert any("stale" in str(e).lower() for e in cleaned_run.errors)
            stale_evs = list(s.scalars(select(Event).where(Event.action == "scan_stale_cleaned")).all())
            assert len(stale_evs) == 1

        # Part B: Start a live scan and verify a second click while running is rejected (409)
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            csrf = _get_csrf(client)
            r_first = client.post(
                "/scan",
                data={"sites": ["mock_alpha"], "csrf_token": csrf},
                headers={"HX-Request": "true"},
            )
            assert r_first.status_code == 200
            assert 'hx-trigger="every 2s"' in r_first.text

            # Second click immediately rejected with 409
            r_second = client.post(
                "/scan",
                data={"sites": ["mock_alpha"], "csrf_token": csrf},
                headers={"HX-Request": "true"},
            )
            assert r_second.status_code == 409
            assert "already running" in r_second.text.lower()

            # Direct CLI `scan` while running also rejected with ScanAlreadyRunningError / exit 1
            from job_hunter.core.skills import SkillMatcher

            with pytest.raises(ScanAlreadyRunningError):
                run_scan(factory, cfg, SkillMatcher(cfg.skills), db_path=db_path, sites=["mock_alpha"])

            _wait_for_scan_completion(client)
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- 6. Stop button


def test_stop_button_terminates_subprocess_and_marks_run_stopped(tmp_path, mock_registry):
    sites = {
        "alljobs": {"enabled": False},
        "mock_alpha": {
            "enabled": True,
            "min_minutes_between_scans": 0,
            "mock_delay_seconds": 8.0,  # would take 24s across 3 roles if not stopped
        },
        "mock_beta": {"enabled": False},
        "mock_gamma": {"enabled": False},
    }
    db_path, cfg_dir, cfg, engine, factory = _make_scan_env(
        tmp_path,
        roles=["Q1", "Q2", "Q3"],
        sites=sites,
    )
    try:
        app = create_app(db_path=db_path, session_factory=factory, cfg=cfg, config_dir=cfg_dir, root=tmp_path)
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            csrf = _get_csrf(client)
            r_start = client.post(
                "/scan",
                data={"sites": ["mock_alpha"], "csrf_token": csrf},
                headers={"HX-Request": "true"},
            )
            assert r_start.status_code == 200

            # Wait briefly until child subprocess writes its active `Run` row
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                with factory() as s:
                    if s.scalar(select(func.count()).select_from(Run)) >= 1:
                        break
                time.sleep(0.05)

            # Live status poll shows Scan disabled and Stop enabled
            poll_resp = client.get("/scan/status", headers={"HX-Request": "true"})
            soup = BeautifulSoup(poll_resp.text, "html.parser")
            scan_btn = soup.find("button", attrs={"form": "scan-start-form"})
            stop_btn = soup.select_one("form[action='/scan/stop'] button[type='submit']")
            assert scan_btn is not None and scan_btn.has_attr("disabled")
            assert stop_btn is not None and not stop_btn.has_attr("disabled")

            t0 = time.monotonic()
            r_stop = client.post(
                "/scan/stop",
                data={"csrf_token": csrf},
                headers={"HX-Request": "true"},
            )
            elapsed = time.monotonic() - t0
            assert r_stop.status_code == 200
            assert elapsed < 4.0  # stopped promptly without waiting 24s!
            assert "Stopped" in r_stop.text
            assert 'hx-trigger="every 2s"' not in r_stop.text
            assert not scan_lock_path(db_path).exists()

            state = read_scan_state(db_path)
            assert state["state"] == "stopped"
            assert state["finished_at"] is not None

            with factory() as s:
                runs = list(s.scalars(select(Run)).all())
                assert len(runs) >= 1
                for r in runs:
                    assert r.finished_at is not None
                    assert any("Stopped by user" in str(e) for e in r.errors)
                stopped_evs = list(s.scalars(select(Event).where(Event.action == "scan_stopped")).all())
                assert len(stopped_evs) >= 1
    finally:
        engine.dispose()


# ------------------------------------------------------------------ 7. Security: CSRF, Origin/Host & Input Validation


def test_scan_csrf_origin_and_input_validation_rejections(tmp_path, mock_registry):
    db_path, cfg_dir, cfg, engine, factory = _make_scan_env(tmp_path)
    try:
        app = create_app(db_path=db_path, session_factory=factory, cfg=cfg, config_dir=cfg_dir, root=tmp_path)
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            # 1. Missing CSRF cookie/token -> 403
            assert client.post("/scan", data={"sites": ["mock_alpha"]}).status_code == 403
            assert client.post("/scan/stop").status_code == 403

            csrf = _get_csrf(client)

            # 2. Wrong CSRF token -> 403
            assert client.post("/scan", data={"sites": ["mock_alpha"], "csrf_token": "wrong"}).status_code == 403
            assert client.post("/scan/stop", data={"csrf_token": "wrong"}).status_code == 403

            # 3. Non-localhost Host or cross-site Origin -> 403
            assert client.post(
                "/scan",
                data={"sites": ["mock_alpha"], "csrf_token": csrf},
                headers={"Origin": "https://evil.attacker.example"},
            ).status_code == 403
            assert client.post(
                "/scan",
                data={"sites": ["mock_alpha"], "csrf_token": csrf},
                headers={"Host": "evil.attacker.example"},
            ).status_code == 403

            # 4. Unregistered site, disabled site, URL or command in extra_query -> 422
            for bad_payload in (
                {"sites": ["https://evil.example/jobs"], "csrf_token": csrf},
                {"sites": ["unknown_site"], "csrf_token": csrf},
                {"sites": ["mock_gamma"], "csrf_token": csrf},  # disabled in config
                {"sites": [], "csrf_token": csrf},
                {"sites": ["mock_alpha"], "extra_query": "https://evil.example/scrape", "csrf_token": csrf},
                {"sites": ["mock_alpha"], "extra_query": "python; whoami", "csrf_token": csrf},
                {"sites": ["mock_alpha"], "extra_query": "--max-pages=999", "csrf_token": csrf},
            ):
                resp = client.post("/scan", data=bad_payload, headers={"HX-Request": "true"})
                assert resp.status_code == 422, f"Expected 422 for payload {bad_payload}"

            # Confirm zero scans ran
            assert read_scan_state(db_path)["state"] == "idle"
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- 8. NEW badge & filter survival logic


def test_new_badge_survives_empty_and_dry_runs_and_updates_on_new_productive_scan(tmp_path, mock_registry):
    sites = {
        "alljobs": {"enabled": False},
        "mock_alpha": {
            "enabled": True,
            "min_minutes_between_scans": 0,
            "mock_jobs": [
                {"id": "a-1", "title": "Junior Backend Alpha", "company": "AlphaCo"},
            ],
        },
        "mock_beta": {
            "enabled": True,
            "min_minutes_between_scans": 0,
            "mock_jobs": [
                {"id": "b-1", "title": "Junior Fullstack Beta", "company": "BetaCo"},
            ],
        },
        "mock_gamma": {"enabled": False},
    }
    db_path, cfg_dir, cfg, engine, factory = _make_scan_env(tmp_path, roles=["Developer"], sites=sites)
    try:
        from job_hunter.core.skills import SkillMatcher

        matcher = SkillMatcher(cfg.skills)
        t1 = datetime(2026, 11, 1, 10, 0, 0)
        t2 = datetime(2026, 11, 1, 12, 0, 0)
        t3 = datetime(2026, 11, 1, 14, 0, 0)
        t4 = datetime(2026, 11, 1, 16, 0, 0)

        # Scan 1 (multi-site: mock_alpha + mock_beta both find 1 new job -> 2 NEW jobs)
        run_scan(factory, cfg, matcher, db_path=db_path, sites=["mock_alpha", "mock_beta"], now=t1)

        app = create_app(db_path=db_path, session_factory=factory, cfg=cfg, config_dir=cfg_dir, root=tmp_path)
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            r1 = client.get("/?new_only=1")
            assert r1.status_code == 200
            assert "Junior Backend Alpha" in r1.text
            assert "Junior Fullstack Beta" in r1.text
            soup1 = BeautifulSoup(r1.text, "html.parser")
            assert len(soup1.select("table.jobs-table .new-badge")) == 2

            # Scan 2 at t2: finds ZERO new jobs (exact duplicates of Scan 1) -> NEW badges MUST SURVIVE!
            run_scan(factory, cfg, matcher, db_path=db_path, sites=["mock_alpha", "mock_beta"], now=t2)
            r2 = client.get("/?new_only=1")
            assert "Junior Backend Alpha" in r2.text
            assert "Junior Fullstack Beta" in r2.text
            assert len(BeautifulSoup(r2.text, "html.parser").select("table.jobs-table .new-badge")) == 2

            # Scan 3 at t3: DRY RUN with brand new job -> writes nothing, NEW badges MUST SURVIVE!
            cfg.search.sites["mock_alpha"]["mock_jobs"] = [
                {"id": "dry-99", "title": "Dry Run Phantom Role", "company": "GhostCo"},
            ]
            run_scan(
                factory,
                cfg,
                matcher,
                db_path=db_path,
                sites=["mock_alpha"],
                dry_run=True,
                now=t3,
            )
            r3 = client.get("/?new_only=1")
            soup3 = BeautifulSoup(r3.text, "html.parser")
            table3 = soup3.select_one("table.jobs-table")
            assert table3 is not None
            table3_text = table3.get_text()
            assert "Dry Run Phantom Role" not in table3_text
            assert "Junior Backend Alpha" in table3_text
            assert "Junior Fullstack Beta" in table3_text
            assert len(soup3.select("table.jobs-table .new-badge")) == 2
            with factory() as s:
                assert s.scalar(select(func.count()).select_from(Job).where(Job.title == "Dry Run Phantom Role")) == 0

            # Scan 4 at t4: real scan finds 1 genuinely new job (`a-2`) -> cutoff advances to t4!
            cfg.search.sites["mock_alpha"]["mock_jobs"] = [
                {"id": "a-1", "title": "Junior Backend Alpha", "company": "AlphaCo"},  # duplicate
                {"id": "a-2", "title": "Junior AI Engineer Fresh", "company": "FreshAI"},  # new!
            ]
            run_scan(factory, cfg, matcher, db_path=db_path, sites=["mock_alpha"], now=t4)

            r4 = client.get("/?new_only=1")
            soup4 = BeautifulSoup(r4.text, "html.parser")
            table4 = soup4.select_one("table.jobs-table")
            assert table4 is not None
            table4_text = table4.get_text()
            assert "Junior AI Engineer Fresh" in table4_text
            assert "Junior Backend Alpha" not in table4_text
            assert "Junior Fullstack Beta" not in table4_text
            assert len(soup4.select("table.jobs-table .new-badge")) == 1
    finally:
        engine.dispose()


# ------------------------------------------------------------------ 9. SQLite WAL concurrency & --db / --config-dir


def test_sqlite_wal_mode_and_concurrent_dashboard_reads_while_subprocess_writes(tmp_path, mock_registry):
    many_jobs = [
        {"id": f"conc-{i}", "title": f"Junior Python Engineer #{i}", "company": f"Co{i}"}
        for i in range(12)
    ]
    sites = {
        "alljobs": {"enabled": False},
        "mock_alpha": {
            "enabled": True,
            "min_minutes_between_scans": 0,
            "mock_delay_seconds": 0.12,
            "mock_jobs": many_jobs[:6],
        },
        "mock_beta": {
            "enabled": True,
            "min_minutes_between_scans": 0,
            "mock_delay_seconds": 0.12,
            "mock_jobs": many_jobs[6:],
        },
        "mock_gamma": {"enabled": False},
    }
    db_path, cfg_dir, cfg, engine, factory = _make_scan_env(
        tmp_path,
        roles=["Query A", "Query B"],
        sites=sites,
    )
    try:
        # 1. Verify WAL journal_mode and busy_timeout pragmas on engine connections
        with engine.connect() as conn:
            journal = str(conn.execute(text("PRAGMA journal_mode")).scalar()).lower()
            busy_ms = int(conn.execute(text("PRAGMA busy_timeout")).scalar())
        assert journal == "wal"
        assert busy_ms >= 30000

        app = create_app(db_path=db_path, session_factory=factory, cfg=cfg, config_dir=cfg_dir, root=tmp_path)
        assert app.state.db_path == db_path.resolve()
        assert app.state.config_dir == cfg_dir

        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            csrf = _get_csrf(client)
            r_start = client.post(
                "/scan",
                data={"sites": ["mock_alpha", "mock_beta"], "csrf_token": csrf},
                headers={"HX-Request": "true"},
            )
            assert r_start.status_code == 200

            # Hammer dashboard read endpoints while the child subprocess is actively writing to SQLite
            read_count = 0
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                r_idx = client.get("/?new_only=1")
                r_cnt = client.get("/partials/counters")
                r_st = client.get("/scan/status", headers={"HX-Request": "true"})
                assert r_idx.status_code == 200
                assert r_cnt.status_code == 200
                assert r_st.status_code == 200
                read_count += 3
                if 'hx-trigger="every 2s"' not in r_st.text:
                    break
                time.sleep(0.03)

            assert read_count >= 6
            final = client.get("/?new_only=1&per_page=50")
            assert final.status_code == 200
            assert "Showing 1–12 of 12 jobs" in final.text
    finally:
        engine.dispose()
