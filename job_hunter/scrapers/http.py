"""Polite HTTP client for scrapers: honest User-Agent, robots.txt enforcement, random 3-8s delays,
per-run page limits, transient retries with backoff, and CAPTCHA/block/login-wall stop detection.
"""

from __future__ import annotations

import logging
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from job_hunter.scrapers.robots import (
    GLOBAL_HARD_DENY_PATH_PREFIXES,
    RobotsDisallowedError,
    RobotsPolicy,
    fetch_robots_policy,
)

log = logging.getLogger("job_hunter.scrapers.http")

DEFAULT_USER_AGENT = "JobHunterPersonal/0.1 (personal job search)"

_BLOCK_STATUS_CODES = frozenset({401, 403, 429})
_RETRY_STATUS_CODES = frozenset({500, 502, 503, 504})

_CHALLENGE_HOSTS = ("perfdrive.com", "shieldsquare.com", "recaptcha.net", "hcaptcha.com")
_LOGIN_PATH_HINTS = ("/auth/", "/auth", "/login", "/signin", "/user/signin", "/account/login")

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_BLOCK_TITLE_TERMS = (
    "captcha",
    "shieldsquare",
    "radware",
    "bot manager",
    "access denied",
    "attention required",
    "just a moment",
    "security check",
    "חסימה",
)
# Active challenge widgets/forms (note: passive `ssConf("cu", "validate.perfdrive.com, ssc")`
# appears in <head> of normal AllJobs pages and must NOT be treated as an active block).
_ACTIVE_CAPTCHA_PATTERNS = (
    re.compile(r'class=["\'][^"\']*\b(?:g-recaptcha|h-captcha|cf-turnstile)\b', re.IGNORECASE),
    re.compile(r'id=["\'](?:cf-chl-widget|px-captcha|challenge-form)["\']', re.IGNORECASE),
    re.compile(r'<form[^>]+action=["\'][^"\']*(?:perfdrive\.com|shieldsquare|/auth/login)', re.IGNORECASE),
    re.compile(r"captcha\.perfdrive\.com", re.IGNORECASE),
)


class ScraperError(RuntimeError):
    """Base error for scraper failures."""


class BlockedError(ScraperError):
    """Bot protection, CAPTCHA, HTTP 401/403/429, login wall, or unexpected page encountered."""


class PageLimitReached(ScraperError):
    """Configured per-run page limit has been reached."""


def detect_block_in_html(
    html: str,
    *,
    url: str = "",
    expected_markers: tuple[str, ...] = (),
) -> str | None:
    """Return a human-readable block reason if `html` is a challenge/login/unexpected page, else None."""
    if not html or not html.strip():
        return f"Empty HTML response at {url}"

    m = _TITLE_RE.search(html[:8192])
    if m:
        title_low = " ".join(m.group(1).lower().split())
        for term in _BLOCK_TITLE_TERMS:
            if term in title_low:
                return f"Challenge/block page title ({title_low!r}) at {url}"

    for pat in _ACTIVE_CAPTCHA_PATTERNS:
        if pat.search(html):
            return f"CAPTCHA/bot-challenge widget detected at {url}"

    if expected_markers and not any(marker in html for marker in expected_markers):
        return f"Unexpected page structure (missing expected content markers) at {url}"

    return None


def _classify_redirect_block(next_url: str) -> str | None:
    parts = urlsplit(next_url)
    host = parts.netloc.lower()
    path = parts.path.lower()
    if any(ch in host for ch in _CHALLENGE_HOSTS):
        return f"Redirected to bot-protection challenge host ({host}): {next_url}"
    if any(path == lp or path.startswith(lp + "/") or path.startswith(lp + ".") for lp in _LOGIN_PATH_HINTS):
        return f"Redirected to login wall: {next_url}"
    return None


@dataclass
class HttpSettings:
    origin: str
    user_agent: str = DEFAULT_USER_AGENT
    delay_min_seconds: float = 3.0
    delay_max_seconds: float = 8.0
    max_pages_per_run: int = 40
    max_retries: int = 2
    backoff_base_seconds: float = 2.0
    timeout_seconds: float = 20.0
    deny_path_prefixes: tuple[str, ...] = GLOBAL_HARD_DENY_PATH_PREFIXES
    deny_substrings: tuple[str, ...] = ()


class PoliteHttpClient:
    """Robots-checked, rate-limited HTTP client with retry/backoff and immediate block halting."""

    def __init__(
        self,
        settings: HttpSettings,
        *,
        client: httpx.Client | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        on_event: Callable[[str, str], None] | None = None,
    ):
        self.settings = settings
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=settings.timeout_seconds, follow_redirects=False)
        self._sleep = sleep_fn
        self._rng = rng or random.Random()
        self._on_event = on_event
        self._robots: RobotsPolicy | None = None
        self.pages_fetched: int = 0

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> PoliteHttpClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _emit(self, action: str, result: str) -> None:
        if self._on_event is not None:
            self._on_event(action, result)

    def ensure_robots(self) -> RobotsPolicy:
        """Fetch and cache robots.txt once at run start; raise RobotsUnavailableError on failure."""
        if self._robots is None:
            self._robots = fetch_robots_policy(
                self._client,
                self.settings.origin,
                self.settings.user_agent,
                deny_path_prefixes=self.settings.deny_path_prefixes,
                deny_substrings=self.settings.deny_substrings,
            )
            delay_info = f" crawl_delay={self._robots.crawl_delay}" if self._robots.crawl_delay else ""
            self._emit("robots_fetched", f"{self.settings.origin}/robots.txt cached{delay_info}")
        return self._robots

    def _polite_delay(self) -> None:
        if self.pages_fetched == 0:
            return
        crawl_delay = self._robots.crawl_delay if self._robots else None
        low = max(self.settings.delay_min_seconds, crawl_delay or 0.0)
        high = max(self.settings.delay_max_seconds, low)
        wait = self._rng.uniform(low, high)
        if wait > 0:
            log.debug("polite sleep %.2fs before next page", wait)
            self._sleep(wait)

    def _backoff_delay(self, attempt: int) -> None:
        wait = self.settings.backoff_base_seconds * (2**attempt) + self._rng.uniform(0.0, 0.5)
        log.warning("transient HTTP failure (attempt %d/%d), backing off %.2fs",
                    attempt + 1, self.settings.max_retries + 1, wait)
        if wait > 0:
            self._sleep(wait)

    def get_html(
        self,
        url: str,
        *,
        expected_markers: tuple[str, ...] = (),
        max_redirects: int = 5,
    ) -> str:
        """Fetch one HTML page after verifying robots.txt, page budget, polite delay, and block checks."""
        robots = self.ensure_robots()

        # Always validate URL against robots.txt BEFORE checking budget or touching network.
        try:
            robots.assert_allowed(url)
        except RobotsDisallowedError as exc:
            self._emit("robots_denied", str(exc))
            raise

        if self.pages_fetched >= self.settings.max_pages_per_run:
            raise PageLimitReached(f"Reached max_pages_per_run={self.settings.max_pages_per_run}")

        self._polite_delay()
        headers = {
            "User-Agent": self.settings.user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "he,en;q=0.8",
        }

        for attempt in range(self.settings.max_retries + 1):
            current_url = url
            try:
                for _hop in range(max_redirects + 1):
                    robots.assert_allowed(current_url)
                    resp = self._client.get(current_url, headers=headers, follow_redirects=False)

                    if resp.status_code in _BLOCK_STATUS_CODES:
                        reason = f"HTTP {resp.status_code} at {current_url}"
                        self._emit("scrape_blocked", reason)
                        raise BlockedError(reason)

                    if resp.status_code in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("location", "").strip()
                        if not loc:
                            raise BlockedError(f"Redirect ({resp.status_code}) with empty Location at {current_url}")
                        next_url = urljoin(current_url, loc)
                        block_reason = _classify_redirect_block(next_url)
                        if block_reason:
                            self._emit("scrape_blocked", block_reason)
                            raise BlockedError(block_reason)
                        try:
                            robots.assert_allowed(next_url)
                        except RobotsDisallowedError as exc:
                            self._emit("robots_denied", str(exc))
                            raise
                        current_url = next_url
                        continue

                    if resp.status_code in _RETRY_STATUS_CODES:
                        # If a 5xx carries an active CAPTCHA/WAF block page, stop immediately without retrying.
                        block_reason = detect_block_in_html(resp.text, url=current_url)
                        if block_reason:
                            full = f"HTTP {resp.status_code} ({block_reason})"
                            self._emit("scrape_blocked", full)
                            raise BlockedError(full)
                        raise _TransientHttpStatusError(f"HTTP {resp.status_code} at {current_url}")

                    if resp.status_code != 200:
                        raise ScraperError(f"Unexpected HTTP {resp.status_code} at {current_url}")

                    html = resp.text
                    block_reason = detect_block_in_html(html, url=current_url, expected_markers=expected_markers)
                    if block_reason:
                        self._emit("scrape_blocked", block_reason)
                        raise BlockedError(block_reason)

                    self.pages_fetched += 1
                    return html

                reason = f"Too many redirects (>{max_redirects}) starting from {url}"
                self._emit("scrape_blocked", reason)
                raise BlockedError(reason)

            except (httpx.TransportError, _TransientHttpStatusError) as exc:
                if attempt < self.settings.max_retries:
                    self._backoff_delay(attempt)
                    continue
                raise ScraperError(
                    f"Request failed after {self.settings.max_retries + 1} attempts for {url}: {exc}"
                ) from exc

        raise ScraperError(f"Unreachable retry loop exit for {url}")  # pragma: no cover


class _TransientHttpStatusError(Exception):
    """Internal marker for retryable 5xx responses."""
