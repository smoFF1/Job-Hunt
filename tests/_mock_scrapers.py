"""Offline mock scrapers for Phase 4 scan & subprocess tests (zero network I/O)."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from job_hunter.core.models import utcnow
from job_hunter.scrapers.base import BaseScraper, JobDetail, RawJob
from job_hunter.scrapers.http import BlockedError
from job_hunter.scrapers.registry import register
from job_hunter.scrapers.robots import RobotsDisallowedError, RobotsUnavailableError


class _ConfigurableMockScraper(BaseScraper):
    """Pure offline scraper driven by `site_config` in test `search.yaml`."""

    requires_http = False

    def __init__(
        self,
        site_config: dict[str, Any] | None = None,
        *,
        now_fn: Callable[[], datetime] = utcnow,
        sleep_fn: Callable[[float], None] = time.sleep,
        should_stop: Callable[[], bool] | None = None,
    ):
        super().__init__(site_config)
        self._now_fn = now_fn
        self._sleep_fn = sleep_fn
        self._should_stop = should_stop
        self.pages_fetched: int = 0
        self._details: dict[str, JobDetail] = {}

    def search(self, query: str) -> list[RawJob]:
        cfg = self.site_config
        rec_file = cfg.get("mock_record_file")
        if rec_file:
            p = Path(str(rec_file))
            p.parent.mkdir(parents=True, exist_ok=True)
            existing: list[str] = []
            if p.exists():
                try:
                    existing = json.loads(p.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    existing = []
            existing.append(query)
            p.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")

        delay = float(cfg.get("mock_delay_seconds", 0.0))
        if delay > 0:
            self._sleep_fn(delay)

        mode = str(cfg.get("mock_mode", "ok"))
        if mode == "captcha":
            raise BlockedError(f"CAPTCHA/bot-challenge widget detected at https://{self.name}.test/search")
        if mode == "http_403":
            raise BlockedError(f"HTTP 403 at https://{self.name}.test/search")
        if mode == "robots_denied":
            raise RobotsDisallowedError(f"Disallowed by robots.txt: https://{self.name}.test/private")
        if mode == "robots_unavailable":
            raise RobotsUnavailableError(f"robots.txt unavailable (503) for https://{self.name}.test")
        if mode == "crash":
            raise RuntimeError(f"Unexpected parser failure in {self.name}")

        per_q = max(1, int(cfg.get("max_pages_per_query", 2)))
        sim_pages = min(per_q, max(1, int(cfg.get("mock_pages_per_query", 1))))
        self.pages_fetched += sim_pages

        now = self._now_fn()
        raw_jobs_cfg = cfg.get("mock_jobs")
        results: list[RawJob] = []

        if isinstance(raw_jobs_cfg, list):
            for item in raw_jobs_cfg:
                jid = str(item["id"])
                url = str(item.get("url") or f"https://{self.name}.test/jobs/{jid}")
                desc = str(
                    item.get("description")
                    or "Entry-level graduate role. 0-1 years experience with Python, FastAPI, Docker, PostgreSQL."
                )
                raw = RawJob(
                    source=self.name,
                    title=str(item["title"]),
                    url=url,
                    company=str(item.get("company") or f"{self.name.title()} Corp"),
                    location=str(item.get("location") or "Tel Aviv"),
                    source_job_id=jid,
                    posted_at=now,
                )
                self._details[url] = JobDetail(url=url, description_text=desc, posted_at=now)
                results.append(raw)
            return results

        # Default: 1 synthetic shortlisted entry-level job per site
        jid = f"{self.name}-default-1"
        url = f"https://{self.name}.test/jobs/{jid}"
        desc = f"Junior developer ({query}). 0-1 years with Python, FastAPI, Docker, Linux, Git, PostgreSQL."
        raw = RawJob(
            source=self.name,
            title=f"Junior Software Developer ({self.name})",
            url=url,
            company=f"{self.name.title()} Labs",
            location="Tel Aviv",
            source_job_id=jid,
            posted_at=now,
        )
        self._details[url] = JobDetail(url=url, description_text=desc, posted_at=now)
        return [raw]

    def fetch_detail(self, url: str) -> JobDetail:
        return self._details[url]


def register_mock_scrapers() -> None:
    @register
    class MockAlphaScraper(_ConfigurableMockScraper):
        name = "mock_alpha"

    @register
    class MockBetaScraper(_ConfigurableMockScraper):
        name = "mock_beta"

    @register
    class MockGammaScraper(_ConfigurableMockScraper):
        name = "mock_gamma"


register_mock_scrapers()
