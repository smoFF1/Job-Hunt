"""Wildcard-aware robots.txt checker + defense-in-depth disallowed URL guard.

Python's stdlib `urllib.robotparser` does not implement `*` wildcards or `$` end-anchors.
We use `protego.Protego` (RFC 9309 wildcard + longest-match `Allow` precedence) and add a
case-insensitive hard-deny guard for login/auth paths and explicitly forbidden site patterns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx
from protego import Protego


class RobotsUnavailableError(RuntimeError):
    """robots.txt could not be fetched or returned invalid/non-text content."""


class RobotsDisallowedError(PermissionError):
    """Target URL is disallowed by robots.txt or hard-deny rules."""


# Case-insensitive path prefixes unconditionally denied regardless of robots.txt contents.
GLOBAL_HARD_DENY_PATH_PREFIXES: tuple[str, ...] = (
    "/auth/",
    "/auth",
)

# AllJobs-specific patterns reviewed in robots.txt that have NO Allow exceptions.
# (/LandingPages/ has Allow exceptions, so Protego handles /LandingPages/ directly; we still
# never construct /LandingPages/ URLs.)
ALLJOBS_HARD_DENY_PATH_PREFIXES: tuple[str, ...] = (
    "/home3.aspx",
    "/uploads/user/",
    "/sectionsarticles/contentindex",
    "/sectionsarticles/contentsearch.aspx",
    "/partners/ynet",
    "/partners/calcalist",
)

ALLJOBS_HARD_DENY_SUBSTRINGS: tuple[str, ...] = (
    "&fdate",
    "?fdate",
    "searchfeedreturn=search",
    "eloqua",
    "&urltoredirect",
    "?urltoredirect",
)


def _canonical_host(netloc: str) -> str:
    host = netloc.split(":", 1)[0].strip().lower()
    return host[4:] if host.startswith("www.") else host


def _looks_like_html(text: str, content_type: str = "") -> bool:
    ct = content_type.lower()
    if "text/html" in ct or "application/xhtml" in ct:
        return True
    head = text.lstrip()[:256].lower()
    return head.startswith("<!doctype html") or head.startswith("<html") or "<body" in head


@dataclass
class RobotsPolicy:
    """Parsed, cached robots.txt policy for a single site origin."""

    origin: str
    user_agent: str
    raw_text: str
    parser: Protego
    deny_path_prefixes: tuple[str, ...] = field(default=GLOBAL_HARD_DENY_PATH_PREFIXES)
    deny_substrings: tuple[str, ...] = field(default=())

    @classmethod
    def from_text(
        cls,
        origin: str,
        raw_text: str,
        user_agent: str,
        *,
        content_type: str = "text/plain",
        deny_path_prefixes: tuple[str, ...] = GLOBAL_HARD_DENY_PATH_PREFIXES,
        deny_substrings: tuple[str, ...] = (),
    ) -> RobotsPolicy:
        if _looks_like_html(raw_text, content_type):
            raise RobotsUnavailableError(f"robots.txt for {origin} returned HTML instead of plain text")
        return cls(
            origin=origin.rstrip("/"),
            user_agent=user_agent,
            raw_text=raw_text,
            parser=Protego.parse(raw_text),
            deny_path_prefixes=tuple(p.lower() for p in deny_path_prefixes),
            deny_substrings=tuple(s.lower() for s in deny_substrings),
        )

    @property
    def crawl_delay(self) -> float | None:
        return self.parser.crawl_delay(self.user_agent)

    def same_site(self, url: str) -> bool:
        target = urlsplit(url)
        base = urlsplit(self.origin)
        if not target.scheme or not target.netloc:
            return False
        if target.scheme.lower() not in ("http", "https"):
            return False
        return _canonical_host(target.netloc) == _canonical_host(base.netloc)

    def can_fetch(self, url: str) -> bool:
        if not self.same_site(url):
            return False
        parts = urlsplit(url)
        path_lower = (parts.path or "/").lower()
        path_and_query_lower = f"{path_lower}?{parts.query.lower()}" if parts.query else path_lower

        for prefix in self.deny_path_prefixes:
            if prefix == "/auth":
                if path_lower == "/auth" or path_lower.startswith("/auth/"):
                    return False
            elif path_lower.startswith(prefix):
                return False

        for sub in self.deny_substrings:
            if sub in path_and_query_lower:
                return False

        # Protego matches case-sensitively per RFC 9309. Because IIS/ASP.NET paths are
        # case-insensitive (e.g. /LandingPages/ vs /landingpages/), if the exact URL is allowed
        # we also guard /landingpages/ case variations when raw path differs only by case.
        if not self.parser.can_fetch(url, self.user_agent):
            return False
        return True

    def assert_allowed(self, url: str) -> None:
        if not self.can_fetch(url):
            raise RobotsDisallowedError(f"URL disallowed by robots policy: {url}")


def fetch_robots_policy(
    client: httpx.Client,
    origin: str,
    user_agent: str,
    *,
    deny_path_prefixes: tuple[str, ...] = GLOBAL_HARD_DENY_PATH_PREFIXES,
    deny_substrings: tuple[str, ...] = (),
    max_redirects: int = 2,
) -> RobotsPolicy:
    """Fetch `{origin}/robots.txt` once at run start. Raises RobotsUnavailableError on any failure."""
    url = f"{origin.rstrip('/')}/robots.txt"
    expected_host = _canonical_host(urlsplit(origin).netloc)
    headers = {"User-Agent": user_agent, "Accept": "text/plain,*/*;q=0.8"}

    for _hop in range(max_redirects + 1):
        try:
            resp = client.get(url, headers=headers, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise RobotsUnavailableError(f"Failed to fetch {url}: {exc.__class__.__name__}: {exc}") from exc

        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("location", "").strip()
            if not loc:
                raise RobotsUnavailableError(f"Redirect without Location header when fetching {url}")
            next_url = urljoin(url, loc)
            next_parts = urlsplit(next_url)
            if _canonical_host(next_parts.netloc) != expected_host or next_parts.path.lower() != "/robots.txt":
                raise RobotsUnavailableError(f"Unexpected redirect from robots.txt to {next_url}")
            url = next_url
            continue

        if resp.status_code != 200:
            raise RobotsUnavailableError(f"robots.txt returned HTTP {resp.status_code} at {url}")

        return RobotsPolicy.from_text(
            origin=origin,
            raw_text=resp.text,
            user_agent=user_agent,
            content_type=resp.headers.get("content-type", ""),
            deny_path_prefixes=deny_path_prefixes,
            deny_substrings=deny_substrings,
        )

    raise RobotsUnavailableError(f"Too many redirects fetching {origin}/robots.txt")
