"""Deduplication keys: fingerprint (normalized company+title+location) and normalized URL."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from job_hunter.core.text import normalize_for_match

_COMPANY_SUFFIX_HE = re.compile(r'בע"?\s?מ')
_COMPANY_SUFFIXES = {"ltd", "limited", "inc", "llc", "corp", "corporation", "co", "plc", "gmbh"}
_CONFIDENTIAL_COMPANIES = {"", "חברה חסויה", "confidential"}
_LOCATION_NOISE = {"israel", "ישראל", "il"}
_NON_WORD = re.compile(r"[^\w]+")
_TRACKING_PARAMS = {"fbclid", "gclid", "msclkid", "mc_cid", "mc_eid", "trk", "trackingid", "refid", "ref"}


def normalize_text(text: str | None) -> str:
    """Lowercase, NFKC, no niqqud/punctuation, single spaces."""
    t = normalize_for_match(text).lower()
    t = _NON_WORD.sub(" ", t).replace("_", " ")
    return " ".join(t.split())


def normalize_company(company: str | None) -> str:
    t = _COMPANY_SUFFIX_HE.sub(" ", normalize_for_match(company))
    words = [w for w in normalize_text(t).split() if w not in _COMPANY_SUFFIXES]
    return " ".join(words)


def normalize_location(location: str | None) -> str:
    words = [w for w in normalize_text(location).split() if w not in _LOCATION_NOISE]
    return " ".join(words)


def is_confidential_company(company: str | None) -> bool:
    """True when company is missing/blank or a generic 'confidential employer' placeholder."""
    return normalize_company(company) in _CONFIDENTIAL_COMPANIES


def fingerprint(
    company: str | None,
    title: str | None,
    location: str | None,
    source_job_id: str | None = None,
) -> str:
    norm_co = normalize_company(company)
    key = f"{norm_co}|{normalize_text(title)}|{normalize_location(location)}"
    if norm_co in _CONFIDENTIAL_COMPANIES and source_job_id and source_job_id.strip():
        key = f"{key}|{source_job_id.strip()}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def normalize_url(url: str | None) -> str | None:
    """Canonical URL for dedup: lowercase scheme/host, no www/fragment/tracking params, sorted query."""
    if not url or not url.strip():
        return None
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    if scheme == "http":
        scheme = "https"
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path or ""
    if len(path) > 1:
        path = path.rstrip("/")
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    )
    return urlunsplit((scheme, host, path, urlencode(query), ""))
