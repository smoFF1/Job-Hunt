"""Offline end-to-end tests for AllJobs scraper, politeness/retries/block stop, and CLI commands."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from job_hunter.cli import main
from job_hunter.core.models import Event, Job, Run
from job_hunter.scrapers.alljobs import ALLJOBS_ORIGIN
from job_hunter.scrapers.http import DEFAULT_USER_AGENT
from job_hunter.scrapers.runner import SiteDisabledError, scrape_site
from tests.conftest import NOW, fake_extractor, write_config
from tests.test_alljobs_parser import require_fixture_file
from tests.test_robots import ALLJOBS_ROBOTS_TXT


def _synthetic_search_html(cards: list[tuple[str, str, str, str, str]], *, has_next: bool = False) -> str:
    """Build realistic AllJobs search-page HTML from `(job_id, title, company, city, desc)` tuples."""
    blocks: list[str] = []
    for jid, title, company, city, desc in cards:
        blocks.append(
            f"""
            <div id="job-box-container{jid}" class="open-board">
              <div class="job-content-top">
                <div class="job-content-top-date">1 ימים</div>
                <div class="job-content-top-title">
                  <div><a href="/Search/UploadSingle.aspx?JobID={jid}"><h2>{title}</h2></a></div>
                  <div class="T14">{company}</div>
                </div>
                <div id="job-body-content{jid}">
                  <div class="job-content-top-location"><b>מיקום המשרה: </b><a>{city}</a></div>
                  <div class="job-content-top-type"><b>סוג משרה: </b>משרה מלאה</div>
                  <div id="job-content-top-acord{jid}" class="job-content-top-acord">
                    <div class="job-content-top-desc AR RTL"><div>{desc}</div></div>
                  </div>
                </div>
              </div>
            </div>
            """
        )
    paging = ""
    if has_next:
        paging = (
            '<div class="jobs-paging-container"><div class="jobs-paging">'
            '<div class="jobs-paging-next"><a href="/SearchResultsGuest.aspx?page=2" title="דף הבא">Next</a></div>'
            "</div></div>"
        )
    return (
        f"<!DOCTYPE html><html><head><title>AllJobs</title></head><body>"
        f'<div id="divAlljobsHeader"></div><div id="divSearchEnginContainer">'
        f'<span id="TotalJobsCount">{len(cards)}</span></div>'
        f"{''.join(blocks)}{paging}</body></html>"
    )


@pytest.fixture
def patch_cv_extractor(monkeypatch):
    import job_hunter.core.cv_loader as cvl

    monkeypatch.setattr(cvl, "extract_pdf_text", fake_extractor)
    monkeypatch.setattr(cvl.sync_cvs, "__defaults__", (fake_extractor, None))


def test_scrape_pipeline_dedup_idempotency_delays_and_user_agent(factory, cfg, matcher, patch_cv_extractor):
    sleeps: list[float] = []
    seen_uas: set[str] = set()

    page1 = _synthetic_search_html(
        [
            ("9001", "Junior Python Backend Developer", "CloudWave Ltd", "תל אביב יפו",
             "0-2 years experience with Python, FastAPI, PostgreSQL and Docker."),
            ("9002", "Senior Software Architect", "BigCorp", "הרצליה",
             "10+ years of system architecture experience."),
            ("9003", "מפתח/ת תוכנה ג'וניור", "חברה חסויה", "חיפה",
             "ללא ניסיון קודם.<br/>דרישות: Python, Linux, Git."),
        ],
        has_next=True,
    )
    page2 = _synthetic_search_html(
        [
            # Duplicate of 9001 on page 2 + a second distinct confidential company job with same title/city!
            ("9001", "Junior Python Backend Developer", "CloudWave Ltd", "תל אביב יפו",
             "0-2 years experience with Python, FastAPI, PostgreSQL and Docker."),
            ("9004", "מפתח/ת תוכנה ג'וניור", "חברה חסויה", "חיפה",
             "בוגרי תואר במדעי המחשב.<br/>ידע ב-Java ו-Docker."),
        ],
        has_next=False,
    )

    def handler(req: httpx.Request) -> httpx.Response:
        seen_uas.add(req.headers.get("user-agent", ""))
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLJOBS_ROBOTS_TXT, headers={"content-type": "text/plain"})
        page = req.url.params.get("page", "1")
        return httpx.Response(200, text=page1 if page == "1" else page2, headers={"content-type": "text/html"})

    transport = httpx.MockTransport(handler)
    summary1 = scrape_site(
        factory,
        cfg,
        matcher,
        "alljobs",
        queries=["Python Developer", "מפתח תוכנה"],
        transport=transport,
        sleep_fn=sleeps.append,
        now=NOW,
    )

    assert seen_uas == {DEFAULT_USER_AGENT}
    # 2 queries * 2 pages = 4 pages -> 3 polite sleeps between pages, all within [3.0, 8.0]
    assert summary1.pages_fetched == 4
    assert len(sleeps) == 3 and all(3.0 <= s <= 8.0 for s in sleeps)
    assert summary1.aborted is False and summary1.errors == []
    assert summary1.unique_postings == 4
    assert summary1.new_count == 4
    assert summary1.shortlisted_count == 3  # 9001 + 9003 + 9004 (both confidential jobs preserved!)
    assert summary1.skipped_count == 1      # 9002 Senior Architect

    # Second identical run must be 100% idempotent (0 new rows, duplicates bumped)
    summary2 = scrape_site(
        factory,
        cfg,
        matcher,
        "alljobs",
        queries=["Python Developer"],
        transport=transport,
        sleep_fn=lambda _: None,
        now=NOW,
    )
    assert summary2.new_count == 0
    assert summary2.duplicate_count == 4

    with factory() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 4
        runs = s.scalars(select(Run).order_by(Run.id)).all()
        assert len(runs) == 2
        assert runs[0].site == "alljobs" and runs[0].new_count == 4 and runs[0].finished_at is not None
        assert runs[1].new_count == 0
        actions = {e.action for e in s.scalars(select(Event)).all()}
        expected_actions = {"run_started", "robots_fetched", "search_query", "job_inserted", "job_duplicate",
                            "run_finished"}
        assert expected_actions <= actions


def test_retry_with_backoff_on_transient_503(factory, cfg, matcher, patch_cv_extractor):
    sleeps: list[float] = []
    attempts = 0
    html = _synthetic_search_html(
        [("9100", "Junior Backend Developer", "RetryCo", "Tel Aviv", "0-1 years Python and Django.")],
        has_next=False,
    )

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLJOBS_ROBOTS_TXT)
        attempts += 1
        if attempts < 3:
            return httpx.Response(503, text="Service Temporarily Unavailable")
        return httpx.Response(200, text=html)

    summary = scrape_site(
        factory,
        cfg,
        matcher,
        "alljobs",
        queries=["Backend"],
        transport=httpx.MockTransport(handler),
        sleep_fn=sleeps.append,
        now=NOW,
    )
    assert attempts == 3
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0] >= 2.0
    assert summary.aborted is False and summary.new_count == 1


@pytest.mark.parametrize(
    "block_response",
    [
        httpx.Response(403, text="Forbidden"),
        httpx.Response(429, text="Too Many Requests"),
        httpx.Response(200, text="<html><head><title>ShieldSquare Captcha</title></head><body></body></html>"),
        httpx.Response(200, text="<html><body><div class='g-recaptcha'></div></body></html>"),
        httpx.Response(302, headers={"location": "https://validate.perfdrive.com/ challenge"}),
        httpx.Response(302, headers={"location": f"{ALLJOBS_ORIGIN}/auth/login"}),
        httpx.Response(200, text="<html><head><title>Login Required</title></head><body>Please sign in</body></html>"),
    ],
)
def test_block_or_captcha_stops_immediately_and_logs_run_and_event(
    factory, cfg, matcher, patch_cv_extractor, block_response: httpx.Response
):
    search_hits = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal search_hits
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLJOBS_ROBOTS_TXT)
        search_hits += 1
        return block_response

    summary = scrape_site(
        factory,
        cfg,
        matcher,
        "alljobs",
        queries=["Query1", "Query2", "Query3"],
        transport=httpx.MockTransport(handler),
        sleep_fn=lambda _: None,
        now=NOW,
    )
    # Stopped on Query1 without attempting Query2 or Query3
    assert search_hits == 1
    assert summary.aborted is True
    assert summary.queries_run == 1
    assert summary.errors

    with factory() as s:
        run = s.scalar(select(Run))
        assert run is not None and run.finished_at is not None and len(run.errors) == 1
        blocked_events = s.scalars(select(Event).where(Event.action.in_(("scrape_blocked", "robots_denied")))).all()
        assert len(blocked_events) >= 1


def test_dry_run_makes_zero_db_writes(tmp_path, cfg, matcher, patch_cv_extractor):
    from job_hunter.core.db import get_engine, init_db, make_session_factory

    db_path = tmp_path / "dry.db"
    engine = get_engine(db_path)
    init_db(engine)
    fac = make_session_factory(engine)
    try:
        html = _synthetic_search_html(
            [("9200", "Junior Fullstack Developer", "DryCo", "Tel Aviv", "Python, React, Node.js, 0-2 years.")],
            has_next=False,
        )

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/robots.txt":
                return httpx.Response(200, text=ALLJOBS_ROBOTS_TXT)
            return httpx.Response(200, text=html)

        summary = scrape_site(
            fac,
            cfg,
            matcher,
            "alljobs",
            dry_run=True,
            queries=["Fullstack"],
            transport=httpx.MockTransport(handler),
            sleep_fn=lambda _: None,
            now=NOW,
        )
        assert summary.dry_run is True
        assert summary.new_count == 1 and summary.shortlisted_count == 1
        assert summary.run_id is None

        with fac() as s:
            assert s.scalar(select(func.count()).select_from(Job)) == 0
            assert s.scalar(select(func.count()).select_from(Run)) == 0
            assert s.scalar(select(func.count()).select_from(Event)) == 0
    finally:
        engine.dispose()


def test_disabled_site_raises(factory, cfg, matcher, tmp_path):
    from job_hunter.config import load_config

    d = write_config(tmp_path / "cfg_disabled", search_overrides={"sites": {"alljobs": {"enabled": False}}})
    disabled_cfg = load_config(d, tmp_path)
    with pytest.raises(SiteDisabledError):
        scrape_site(factory, disabled_cfg, matcher, "alljobs")


def test_cli_import_html_synthetic_and_dry_run(project, tmp_path, patch_cv_extractor, capsys):
    db_path = tmp_path / "import.db"
    saved_page = tmp_path / "saved_search.html"
    saved_page.write_text(
        _synthetic_search_html(
            [
                ("9301", "Junior AI Developer", "BrainLab", "Herzliya",
                 "Graduates welcome. Python, FastAPI, Docker, LLM."),
                ("9302", "VP Engineering", "ExecCorp", "Tel Aviv", "15 years managing R&D."),
            ]
        ),
        encoding="utf-8",
    )

    # 1. Dry-run before DB even exists -> does not create DB file
    code = main([
        "--db", str(db_path),
        "--config-dir", str(project / "config"),
        "--root", str(project),
        "import-html", "alljobs", str(saved_page), "--dry-run",
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "[DRY RUN] Import HTML (alljobs)" in out
    assert "Junior AI Developer" in out
    assert "VP Engineering" not in out
    assert not db_path.exists()

    # 2. Real import -> writes jobs, runs, events
    code = main([
        "--db", str(db_path),
        "--config-dir", str(project / "config"),
        "--root", str(project),
        "import-html", "alljobs", str(saved_page),
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "new=2" in out and "shortlisted=1" in out and "skipped=1" in out
    assert db_path.exists()

    # 3. Re-importing same file is idempotent
    code = main([
        "--db", str(db_path),
        "--config-dir", str(project / "config"),
        "--root", str(project),
        "import-html", "alljobs", str(saved_page),
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "new=0" in out and "duplicates=2" in out


def test_cli_import_html_with_saved_fixtures(project, tmp_path, patch_cv_extractor, capsys):
    p1: Path = require_fixture_file("search_page_1.html")
    p2: Path = require_fixture_file("search_page_2.html")
    db_path = tmp_path / "fixtures_import.db"

    code1 = main([
        "--db", str(db_path),
        "--config-dir", str(project / "config"),
        "--root", str(project),
        "import-html", "alljobs", str(p1), "--dry-run",
    ])
    out1 = capsys.readouterr().out
    assert code1 == 0 and "[DRY RUN]" in out1 and "postings=15" in out1
    assert "Infinity Labs" not in out1

    code2 = main([
        "--db", str(db_path),
        "--config-dir", str(project / "config"),
        "--root", str(project),
        "import-html", "alljobs", str(p1),
    ])
    out2 = capsys.readouterr().out
    assert code2 == 0 and "new=15" in out2

    code3 = main([
        "--db", str(db_path),
        "--config-dir", str(project / "config"),
        "--root", str(project),
        "import-html", "alljobs", str(p2),
    ])
    out3 = capsys.readouterr().out
    assert code3 == 0 and "postings=15" in out3


def test_cli_scrape_alljobs_offline(project, tmp_path, patch_cv_extractor, monkeypatch, capsys):
    import job_hunter.cli as cli_mod
    import job_hunter.scrapers.runner as runner_mod

    html = _synthetic_search_html(
        [("9401", "Junior Software Engineer", "CliCo", "Tel Aviv", "0-1 years Python, Git, Linux.")],
        has_next=False,
    )

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLJOBS_ROBOTS_TXT)
        return httpx.Response(200, text=html)

    transport = httpx.MockTransport(handler)
    orig_scrape = runner_mod.scrape_site

    def offline_scrape(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        kwargs.setdefault("sleep_fn", lambda _: None)
        return orig_scrape(*args, **kwargs)

    monkeypatch.setattr(cli_mod, "scrape_site", offline_scrape)
    db_path = tmp_path / "cli_scrape.db"

    code_dry = main([
        "--db", str(db_path),
        "--config-dir", str(project / "config"),
        "--root", str(project),
        "scrape", "alljobs", "--dry-run", "--query", "Junior Developer", "--max-pages", "1",
    ])
    out_dry = capsys.readouterr().out
    assert code_dry == 0 and "[DRY RUN] Scrape (alljobs)" in out_dry and not db_path.exists()

    code_live = main([
        "--db", str(db_path),
        "--config-dir", str(project / "config"),
        "--root", str(project),
        "scrape", "alljobs", "--query", "Junior Developer", "--max-pages", "1",
    ])
    out_live = capsys.readouterr().out
    assert code_live == 0 and "Scrape (alljobs) run=#1" in out_live and "new=1" in out_live


def test_missing_fixtures_dir_skips_cleanly(monkeypatch):
    import tests.test_alljobs_parser as parser_tests

    monkeypatch.setattr(parser_tests, "find_alljobs_fixtures_dir", lambda: None)
    with pytest.raises(pytest.skip.Exception):
        parser_tests.require_fixture_file("search_page_1.html")
