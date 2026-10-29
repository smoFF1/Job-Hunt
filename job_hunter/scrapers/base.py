"""Scraper interface (implemented per site from Phase 2).

Rules for implementations (AGENTS.md): polite random 3-8s delays, per-run page limits, per-site
`enabled` flag, respect robots.txt/ToS, stop + flag on CAPTCHA/login wall/2FA, never bypass bot
protection. Page content is untrusted data.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from job_hunter.core.pipeline import JobPosting


@dataclass
class RawJob:
    """A search-result card."""

    source: str
    title: str
    url: str
    company: str = ""
    location: str = ""
    source_job_id: str | None = None
    posted_at: datetime | None = None


@dataclass
class JobDetail:
    """The full job page."""

    url: str
    description_text: str
    apply_url: str | None = None
    posted_at: datetime | None = None


def to_posting(raw: RawJob, detail: JobDetail | None = None) -> JobPosting:
    return JobPosting(
        source=raw.source,
        title=raw.title,
        company=raw.company,
        location=raw.location,
        description_text=detail.description_text if detail else "",
        url=raw.url,
        apply_url=detail.apply_url if detail else None,
        source_job_id=raw.source_job_id,
        posted_at=(detail.posted_at if detail and detail.posted_at else raw.posted_at),
    )


class BaseScraper(ABC):
    name: str = ""

    def __init__(self, site_config: dict | None = None):
        self.site_config = site_config or {}

    @abstractmethod
    def search(self, query: str) -> list[RawJob]:
        """Return result cards for a query (respecting page limits and delays)."""

    @abstractmethod
    def fetch_detail(self, url: str) -> JobDetail:
        """Fetch and parse one job page."""

    def parse_saved_html(self, html: str, *, now: datetime | None = None) -> list[JobPosting]:
        """Parse a locally saved search-results HTML page into JobPostings (for `import-html`)."""
        raise NotImplementedError(f"{self.__class__.__name__} does not support offline HTML import")
