"""Text normalization and keyword matching shared by filters, categories and scoring.

Handles English and Hebrew equally: NFKC, Hebrew niqqud / bidi marks stripped, geresh and
gershayim unified with ASCII quotes, whitespace collapsed.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

_NIQQUD = re.compile(r"[\u0591-\u05C7]")
_BIDI = re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
_APOSTROPHES = str.maketrans({"\u05f3": "'", "\u2019": "'", "\u2018": "'", "`": "'", "\u00b4": "'",
                              "\u05f4": '"', "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-"})
_HEBREW_CHAR = re.compile(r"[\u05d0-\u05ea]")
_LATIN_CHAR = re.compile(r"[A-Za-z]")


def normalize_for_match(text: str | None, keep_newlines: bool = False) -> str:
    """Normalize text for matching. Case is preserved (patterns decide case sensitivity)."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = _BIDI.sub("", t)
    t = _NIQQUD.sub("", t)
    t = t.translate(_APOSTROPHES)
    if keep_newlines:
        lines = [" ".join(line.split()) for line in t.splitlines()]
        return "\n".join(line for line in lines if line)
    return " ".join(t.split())


def has_hebrew(text: str) -> bool:
    return bool(_HEBREW_CHAR.search(text))


def detect_language(text: str | None) -> str:
    """'he', 'en' or 'mixed' by the share of Hebrew letters among Hebrew+Latin letters."""
    if not text:
        return "en"
    he = len(_HEBREW_CHAR.findall(text))
    lat = len(_LATIN_CHAR.findall(text))
    if he + lat == 0:
        return "en"
    ratio = he / (he + lat)
    if ratio >= 0.6:
        return "he"
    if ratio <= 0.25:
        return "en"
    return "mixed"


def _keyword_piece(keyword: str, hebrew_prefixes: str) -> str:
    kw = normalize_for_match(keyword)
    piece = re.escape(kw)
    # spaces and hyphens are interchangeable ("entry level" == "entry-level")
    piece = re.sub(r"(?:\\ |\\-|-| )+", r"[\\s\\-]+", piece)
    if has_hebrew(kw) and hebrew_prefixes:
        piece = f"[{re.escape(hebrew_prefixes)}]{{0,2}}{piece}"
    return piece


class KeywordMatcher:
    """Whole-word, case-insensitive matcher for a keyword list (EN + HE).

    Hebrew keywords also match with up to two one-letter prefixes (ו/ה/ל/ב/ש by default).
    """

    def __init__(self, keywords: Iterable[str], hebrew_prefixes: str = "ובהלש"):
        kws = sorted({k for k in keywords if k and k.strip()}, key=len, reverse=True)
        self.keywords = kws
        if not kws:
            self._re = None
            return
        alternation = "|".join(_keyword_piece(k, hebrew_prefixes) for k in kws)
        self._re = re.compile(rf"(?<!\w)(?:{alternation})(?!\w)", re.IGNORECASE)

    def search(self, text: str | None) -> str | None:
        """Return the first matched text (as it appears, normalized), or None."""
        if self._re is None or not text:
            return None
        m = self._re.search(normalize_for_match(text))
        return m.group(0) if m else None

    def findall(self, text: str | None) -> list[str]:
        if self._re is None or not text:
            return []
        return [m.group(0) for m in self._re.finditer(normalize_for_match(text))]

    def __bool__(self) -> bool:
        return self._re is not None
