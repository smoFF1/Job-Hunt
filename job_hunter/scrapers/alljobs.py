"""AllJobs (alljobs.co.il) guest search scraper and offline HTML parser.

Phase 2 design:
- Search results pages (`SearchResultsGuest.aspx`) embed full job descriptions server-side,
  so `search()` caches each card's `JobDetail` and `fetch_detail()` reads from that cache
  without making per-job network requests (`UploadSingle.aspx` is never fetched live).
- No login, no cookies/credentials, no `/auth/*`, and strict `robots.txt` enforcement.
- All HTML is treated as untrusted data: parsed into plain text only via BeautifulSoup.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

from bs4 import BeautifulSoup, Tag

from job_hunter.core.dedup import normalize_url
from job_hunter.core.models import utcnow
from job_hunter.core.pipeline import JobPosting
from job_hunter.scrapers.base import BaseScraper, JobDetail, RawJob, to_posting
from job_hunter.scrapers.http import (
     DEFAULT_USER_AGENT,
    HttpSettings,
    PageLimitReached,
    PoliteHttpClient,
)
from job_hunter.scrapers.registry import register
from job_hunter.scrapers.robots import (
    ALLJOBS_HARD_DENY_PATH_PREFIXES,
    ALLJOBS_HARD_DENY_SUBSTRINGS,
    GLOBAL_HARD_DENY_PATH_PREFIXES,
)

log = logging.getLogger("job_hunter.scrapers.alljobs")

ALLJOBS_ORIGIN = "https://www.alljobs.co.il"
EXPECTED_SEARCH_MARKERS: tuple[str, ...] = (
    "job-box-container",
    "divSearchEnginContainer",
    "TotalJobsCount",
    "divAlljobsHeader",
)

_CARD_ID_RE = re.compile(r"^job-box-container(\d+)$")
_JOB_ID_DIGITS_RE = re.compile(r"^\d+$")
_LOC_PREFIX_RE = re.compile(r"^(?:מיקום המשרה|location)\s*:\s*", re.IGNORECASE)
_INLINE_WS_RE = re.compile(r"[ \t\r\f\v\xa0]+")

_MINUTES_RE = re.compile(r"(?:לפני\s*)?(\d+)\s*דקות")
_HOURS_RE = re.compile(r"(?:לפני\s*)?(\d+)\s*שעות")
_DAYS_RE = re.compile(r"(?:לפני\s*)?(\d+)\s*ימים")
_EXPLICIT_DATE_RE = re.compile(r"^(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})$")

_BLOCK_TAGS = frozenset({"div", "p", "li", "ul", "ol", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section"})


def build_alljobs_http_settings(site_config: dict | None = None) -> HttpSettings:
    cfg = site_config or {}
    return HttpSettings(
        origin=ALLJOBS_ORIGIN,
        user_agent=str(cfg.get("user_agent") or DEFAULT_USER_AGENT),
        delay_min_seconds=float(cfg.get("delay_min_seconds", 3.0)),
        delay_max_seconds=float(cfg.get("delay_max_seconds", 8.0)),
        max_pages_per_run=int(cfg.get("max_pages_per_run", 40)),
        max_retries=int(cfg.get("max_retries", 2)),
        backoff_base_seconds=float(cfg.get("backoff_base_seconds", 2.0)),
        timeout_seconds=float(cfg.get("timeout_seconds", 20.0)),
        deny_path_prefixes=GLOBAL_HARD_DENY_PATH_PREFIXES + ALLJOBS_HARD_DENY_PATH_PREFIXES,
        deny_substrings=ALLJOBS_HARD_DENY_SUBSTRINGS,
    )


def build_search_url(query: str, page: int = 1) -> str:
    """Build a safe guest search URL without any disallowed parameters."""
    q = (query or "").strip()
    if not q:
        raise ValueError("Search query must not be empty")
    if page < 1:
        raise ValueError(f"Page number must be >= 1, got {page}")
    params = urlencode(
        [
            ("page", str(page)),
            ("position", ""),
            ("type", ""),
            ("freetxt", q),
            ("city", ""),
            ("region", ""),
        ]
    )
    return f"{ALLJOBS_ORIGIN}/SearchResultsGuest.aspx?{params}"


def build_detail_url(job_id: str) -> str:
    """Canonical job URL stored on RawJob/JobPosting (never fetched live in Phase 2)."""
    jid = (job_id or "").strip()
    if not _JOB_ID_DIGITS_RE.match(jid):
        raise ValueError(f"Invalid numeric AllJobs JobID: {job_id!r}")
    return f"{ALLJOBS_ORIGIN}/Search/UploadSingle.aspx?JobID={jid}"


def extract_job_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    qs = parse_qs(urlsplit(url).query)
    for key, vals in qs.items():
        if key.lower() == "jobid" and vals:
            cand = vals[0].strip()
            if _JOB_ID_DIGITS_RE.match(cand):
                return cand
    return None


def _clean_inline(text: str | None) -> str:
    if not text:
        return ""
    return _INLINE_WS_RE.sub(" ", text).strip()


def _extract_multiline_text(root: Tag) -> str:
    """Convert untrusted HTML fragment into clean newline-preserving plain text."""
    # Work on a shallow soup copy of the fragment so callers' trees are not mutated unexpectedly.
    frag = BeautifulSoup(str(root), "html.parser")
    for bad in frag.find_all(["script", "style", "noscript", "template"]):
        bad.decompose()
    # Strip AllJobs' trailing employer promo link ("לעוד משרות ומידע על ... >") if nested inside.
    for a in frag.find_all("a", href=True):
        if "/employer/hp/" in a["href"].lower():
            a.decompose()
    for br in frag.find_all("br"):
        br.replace_with("\n")
    for tag in frag.find_all(_BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")

    raw = frag.get_text()
    lines = [_clean_inline(line) for line in raw.splitlines()]
    cleaned: list[str] = []
    for line in lines:
        if line:
            cleaned.append(line)
    return "\n".join(cleaned)


def parse_posted_at(raw_date: str | None, now: datetime) -> datetime | None:
    """Parse AllJobs Hebrew relative dates and DD/MM/YYYY dates into naive timestamps."""
    s = _clean_inline(raw_date)
    if not s:
        return None

    if s in ("היום", "עכשיו", "לפני דקה"):
        return now
    if s in ("אתמול", "יום 1", "לפני יום"):
        return now - timedelta(days=1)
    if s in ("יומיים", "לפני יומיים"):
        return now - timedelta(days=2)
    if s in ("לפני שעה", "שעה 1"):
        return now - timedelta(hours=1)
    if s == "לפני שעתיים":
        return now - timedelta(hours=2)

    if m := _MINUTES_RE.search(s):
        return now - timedelta(minutes=int(m.group(1)))
    if m := _HOURS_RE.search(s):
        return now - timedelta(hours=int(m.group(1)))
    if m := _DAYS_RE.search(s):
        return now - timedelta(days=int(m.group(1)))
    if m := _EXPLICIT_DATE_RE.match(s):
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 100:
            year += 2000
        try:
            return datetime(year, month, day)
        except ValueError:
            return None
    return None


def _is_vip_card(card: Tag) -> bool:
    classes = {c.lower() for c in (card.get("class") or [])}
    if "organic-board" in classes:
        return True
    return card.select_one(".job-button-send-vip") is not None


def _extract_card_job_id(card: Tag) -> str | None:
    card_id = str(card.get("id") or "")
    if m := _CARD_ID_RE.match(card_id):
        return m.group(1)
    hidden = card.select_one(".jobid-hidden")
    if hidden:
        cand = _clean_inline(hidden.get_text())
        if _JOB_ID_DIGITS_RE.match(cand):
            return cand
    link = card.select_one('a[href*="UploadSingle.aspx"]')
    if link:
        return extract_job_id_from_url(str(link.get("href") or ""))
    return None


def _extract_location(card: Tag) -> str:
    loc_div = card.select_one('div[class*="job-content-top-location"]')
    if loc_div is None:
        return ""
    regions = loc_div.select_one('div[class*="job-regions-content"]')
    if regions is not None:
        cities: list[str] = []
        for a in regions.find_all("a"):
            c = _clean_inline(a.get_text(" ", strip=True))
            if c and c not in cities:
                cities.append(c)
        if cities:
            return ", ".join(cities)

    inline_links = [_clean_inline(a.get_text(" ", strip=True)) for a in loc_div.find_all("a")]
    inline_cities = [c for c in inline_links if c]
    if inline_cities:
        return ", ".join(dict.fromkeys(inline_cities))

    text = _clean_inline(loc_div.get_text(" ", strip=True))
    return _LOC_PREFIX_RE.sub("", text).strip()


def _extract_description(card: Tag, job_id: str) -> str:
    acord = card.find(id=f"job-content-top-acord{job_id}") or card.select_one('div[class*="job-content-top-acord"]')
    if acord is None:
        return ""

    desc_divs = acord.select('div[class*="job-content-top-desc"]')
    chosen: Tag | None = None
    for d in desc_divs:
        cls_tokens = {c.upper() for c in (d.get("class") or [])}
        if "RTL" in cls_tokens or "LTR" in cls_tokens:
            chosen = d
            break
    if chosen is None and desc_divs:
        chosen = desc_divs[0]
    if chosen is None:
        return _extract_multiline_text(acord)
    return _extract_multiline_text(chosen)


@dataclass
class ParsedSearchPage:
    items: list[tuple[RawJob, JobDetail]]
    has_next_page: bool
    total_cards: int
    vip_skipped: int


def parse_search_page(
    html: str,
    *,
    include_vip: bool = False,
    now: datetime | None = None,
) -> ParsedSearchPage:
    """Parse an AllJobs `SearchResultsGuest.aspx` HTML document into `(RawJob, JobDetail)` pairs."""
    ref_now = now or utcnow()
    soup = BeautifulSoup(html, "html.parser")

    items: list[tuple[RawJob, JobDetail]] = []
    total_cards = 0
    vip_skipped = 0

    for card in soup.find_all("div", id=_CARD_ID_RE):
        total_cards += 1
        if not include_vip and _is_vip_card(card):
            vip_skipped += 1
            continue

        job_id = _extract_card_job_id(card)
        if not job_id:
            log.warning("skipping AllJobs card without numeric JobID")
            continue

        title_div = card.select_one('div[class*="job-content-top-title"]')
        title_link = (
            title_div.select_one('a[href*="UploadSingle.aspx"]') if title_div else None
        ) or card.select_one('a[href*="UploadSingle.aspx"]')
        if title_link is None:
            log.warning("skipping AllJobs card %s without title link", job_id)
            continue

        heading = title_link.find(["h1", "h2", "h3"])
        title = _clean_inline((heading or title_link).get_text(" ", strip=True))
        if not title:
            log.warning("skipping AllJobs card %s with empty title", job_id)
            continue

        company = ""
        if title_div is not None:
            co_div = title_div.select_one(".T14")
            if co_div is not None:
                company = _clean_inline(co_div.get_text(" ", strip=True))

        location = _extract_location(card)
        date_div = card.select_one(".job-content-top-date")
        posted_at = parse_posted_at(date_div.get_text(" ", strip=True) if date_div else None, ref_now)
        description = _extract_description(card, job_id)
        detail_url = build_detail_url(job_id)

        raw = RawJob(
            source="alljobs",
            title=title,
            url=detail_url,
            company=company,
            location=location,
            source_job_id=job_id,
            posted_at=posted_at,
        )
        detail = JobDetail(
            url=detail_url,
            description_text=description,
            apply_url=None,
            posted_at=posted_at,
        )
        items.append((raw, detail))

    has_next = soup.select_one(".jobs-paging-next a[href]") is not None
    return ParsedSearchPage(
        items=items,
        has_next_page=has_next,
        total_cards=total_cards,
        vip_skipped=vip_skipped,
    )


@register
class AllJobsScraper(BaseScraper):
    """Scraper for alljobs.co.il public guest search listings."""

    name = "alljobs"

    def __init__(
        self,
        site_config: dict | None = None,
        *,
        http_client: PoliteHttpClient | None = None,
        now_fn: Callable[[], datetime] = utcnow,
    ):
        super().__init__(site_config)
        self._owns_http = http_client is None
        self.http = http_client or PoliteHttpClient(build_alljobs_http_settings(self.site_config))
        self._now_fn = now_fn
        self._detail_cache: dict[str, JobDetail] = {}

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    @property
    def include_vip(self) -> bool:
        return bool(self.site_config.get("include_vip", False))

    @property
    def max_pages_per_query(self) -> int:
        return max(1, int(self.site_config.get("max_pages_per_query", 2)))

    def _cache_detail(self, raw: RawJob, detail: JobDetail) -> None:
        self._detail_cache[raw.url] = detail
        norm = normalize_url(raw.url)
        if norm:
            self._detail_cache[norm] = detail
        if raw.source_job_id:
            self._detail_cache[f"jobid:{raw.source_job_id}"] = detail

    def search(self, query: str) -> list[RawJob]:
        """Fetch up to `max_pages_per_query` guest search pages for `query` and cache card details."""
        results: list[RawJob] = []
        seen_ids: set[str] = set()
        now = self._now_fn()

        for page in range(1, self.max_pages_per_query + 1):
            url = build_search_url(query, page=page)
            try:
                html = self.http.get_html(url, expected_markers=EXPECTED_SEARCH_MARKERS)
            except PageLimitReached:
                log.info("stopping query %r at page %d: run page budget reached", query, page)
                break

            parsed = parse_search_page(html, include_vip=self.include_vip, now=now)
            for raw, detail in parsed.items:
                self._cache_detail(raw, detail)
                key = raw.source_job_id or raw.url
                if key not in seen_ids:
                    seen_ids.add(key)
                    results.append(raw)

            if parsed.total_cards == 0 or not parsed.has_next_page:
                break

        return results

    def fetch_detail(self, url: str) -> JobDetail:
        """Return the cached `JobDetail` parsed during `search()`. Never performs a network request."""
        if url in self._detail_cache:
            return self._detail_cache[url]
        norm = normalize_url(url)
        if norm and norm in self._detail_cache:
            return self._detail_cache[norm]
        jid = extract_job_id_from_url(url)
        if jid and f"jobid:{jid}" in self._detail_cache:
            return self._detail_cache[f"jobid:{jid}"]
        raise LookupError(
            f"JobDetail for {url!r} is not cached from search(); "
            "live UploadSingle.aspx fetching is disabled in Phase 2"
        )

    def parse_saved_html(self, html: str, *, now: datetime | None = None) -> list[JobPosting]:
        """Parse a locally saved AllJobs search-results HTML file into `JobPosting` objects."""
        parsed = parse_search_page(html, include_vip=self.include_vip, now=now or self._now_fn())
        postings: list[JobPosting] = []
        for raw, detail in parsed.items:
            self._cache_detail(raw, detail)
            postings.append(to_posting(raw, detail))
        return postings
