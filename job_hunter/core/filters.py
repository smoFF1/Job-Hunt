"""Deterministic skip filters (SPEC "Filtering spec"): senior title, student-only, experience.

Decisions (Phase 1 plan):
- Q1: sentences/lines marked as advantage/preferred/יתרון are ignored for years extraction; the
  required minimum is the MAX over the remaining mentions (a range contributes its lower bound).
- Q2: the student-only check looks at the TITLE; a graduate/בוגר keyword anywhere un-skips it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from job_hunter.config import SearchConfig
from job_hunter.core.text import KeywordMatcher, normalize_for_match

# ----------------------------------------------------------------------------- experience

_NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10,
    "שלוש": 3, "שלושה": 3, "ארבע": 4, "ארבעה": 4, "חמש": 5, "חמישה": 5, "שש": 6, "שישה": 6,
}
_NUM = r"(\d{1,2}(?:\.\d)?|" + "|".join(sorted(_NUM_WORDS, key=len, reverse=True)) + r")"
_B = r"(?<![\w.])"  # left boundary before a number
_YEARS = r"(?:years?|yrs?|שנות|שנים|שנה)(?!\w)"
_NOT_AGO = r"(?!\s+ago)"
_EXP_WORD = re.compile(r"(?<!\w)(?:experience|exp|ניסיון|נסיון)(?!\w)", re.IGNORECASE)

# (pattern, needs_experience_word). Applied in order; matched spans are masked before the next
# pattern so "2-4 years" isn't also read as "4 years".
_EXPERIENCE_PATTERNS: list[tuple[re.Pattern[str], bool]] = [
    # ranges: "2-4 years", "2 to 4 yrs", "between 2 and 4 years", "בין 2 ל-4 שנים"
    (re.compile(rf"{_B}{_NUM}\s*\+?\s*(?:-|to|עד)\s*{_NUM}\s*\+?\s*{_YEARS}{_NOT_AGO}", re.I), False),
    (re.compile(rf"(?:between|בין)\s+{_NUM}\s*(?:and|ל-?)\s*{_NUM}\s*\+?\s*{_YEARS}", re.I), False),
    # lead words: "at least 3 years", "minimum of 2 yrs", "לפחות 3 שנות", "יותר מ-2 שנים"
    (re.compile(rf"(?:at\s+least|minimum(?:\s+of)?|min\.?|over|more\s+than|לפחות|מינימום|מעל|יותר\s+מ-?)"
                rf"\s*{_NUM}\s*\+?\s*{_YEARS}", re.I), False),
    # plus: "3+ years", "+3 שנות"
    (re.compile(rf"{_B}{_NUM}\s*\+\s*{_YEARS}", re.I), False),
    (re.compile(rf"\+\s*{_NUM}\s*{_YEARS}", re.I), False),
    # bare: "3 years of experience", "3 שנות ניסיון", "experience of 2 years" (needs an experience word)
    (re.compile(rf"{_B}{_NUM}\s*{_YEARS}{_NOT_AGO}", re.I), True),
]
# Hebrew word forms (need an experience word in the same sentence)
_HE_WORD_FORMS: list[tuple[re.Pattern[str], float]] = [
    (re.compile(r"(?<!\w)[ול]?שנתיים(?!\w)"), 2),
    (re.compile(r"(?<!\w)[וכ]?שנת\s+ניסיון(?!\w)"), 1),
    (re.compile(r"(?<!\w)[וכ]?שנה\s+(?:של\s+)?(?:ניסיון|נסיון)(?!\w)"), 1),
    (re.compile(r"(?:ניסיון|נסיון)\s+של\s+(?:כ-?)?שנה(?!\w)"), 1),
]
_SEGMENT_SPLIT = re.compile(r"\n+|[•·▪●;]|(?<=[.!?])\s+")


def _num(token: str) -> float | None:
    token = token.lower()
    if token in _NUM_WORDS:
        return float(_NUM_WORDS[token])
    try:
        return float(token)
    except ValueError:
        return None


def _mask(text: str, span: tuple[int, int]) -> str:
    a, b = span
    return text[:a] + " " * (b - a) + text[b:]


@dataclass(frozen=True)
class ExperienceMention:
    years: float
    text: str


def split_segments(text: str) -> list[str]:
    return [s.strip() for s in _SEGMENT_SPLIT.split(text) if s and s.strip()]


def extract_experience_mentions(segment: str) -> list[ExperienceMention]:
    found: list[ExperienceMention] = []
    work = segment
    has_exp_word = bool(_EXP_WORD.search(segment))
    for pattern, needs_exp in _EXPERIENCE_PATTERNS:
        if needs_exp and not has_exp_word:
            continue
        for m in list(pattern.finditer(work)):
            n = _num(m.group(1))
            if n is not None and n <= 30:
                found.append(ExperienceMention(n, m.group(0).strip()))
            work = _mask(work, m.span())
    if has_exp_word:
        for pattern, years in _HE_WORD_FORMS:
            for m in list(pattern.finditer(work)):
                found.append(ExperienceMention(years, m.group(0).strip()))
                work = _mask(work, m.span())
    return found


# ----------------------------------------------------------------------------- filters

@dataclass(frozen=True)
class ExperienceResult:
    years_min: float | None
    evidence: str | None
    ignored: list[str]  # mentions skipped because they were in advantage/nice-to-have context


@dataclass(frozen=True)
class FilterDecision:
    skip: bool
    reason: str | None  # e.g. "Skipped - Senior Role (matched: 'senior')"
    rule: str | None  # 'senior' | 'student' | 'experience'
    years_min: float | None
    experience_evidence: str | None


class JobFilters:
    def __init__(self, cfg: SearchConfig):
        self.cfg = cfg
        p = cfg.hebrew_prefixes
        self.senior = KeywordMatcher(cfg.senior_title_keywords, p)
        self.student = KeywordMatcher(cfg.student_keywords, p)
        self.graduate = KeywordMatcher(cfg.graduate_keywords, p)
        self.advantage = KeywordMatcher(cfg.advantage_markers, p)

    def senior_match(self, title: str) -> str | None:
        return self.senior.search(title)

    def student_only_match(self, title: str, description: str) -> str | None:
        hit = self.student.search(title)
        if not hit:
            return None
        if self.graduate.search(title) or self.graduate.search(description):
            return None
        return hit

    def extract_experience(self, description: str) -> ExperienceResult:
        text = normalize_for_match(description, keep_newlines=True)
        best: ExperienceMention | None = None
        ignored: list[str] = []
        in_advantage_block = False
        for seg in split_segments(text):
            is_header = seg.endswith(":")
            marked = bool(self.advantage.search(seg))
            if is_header:
                in_advantage_block = marked
            mentions = extract_experience_mentions(seg)
            if marked or in_advantage_block:
                ignored.extend(m.text for m in mentions)
                continue
            for m in mentions:
                if best is None or m.years > best.years:
                    best = m
        return ExperienceResult(best.years if best else None, best.text if best else None, ignored)

    def evaluate(self, title: str, description: str) -> FilterDecision:
        title = title or ""
        description = description or ""
        exp = self.extract_experience(description)

        hit = self.senior_match(title)
        if hit:
            return FilterDecision(True, f"Skipped - Senior Role (matched: '{hit}')", "senior",
                                  exp.years_min, exp.evidence)
        hit = self.student_only_match(title, description)
        if hit:
            return FilterDecision(True, f"Skipped - Student Position (matched: '{hit}')", "student",
                                  exp.years_min, exp.evidence)
        if exp.years_min is not None and exp.years_min >= self.cfg.experience_cutoff:
            return FilterDecision(
                True,
                f"Skipped - Experience ({_fmt_years(exp.years_min)}+ years required, "
                f"cutoff {_fmt_years(self.cfg.experience_cutoff)}; matched: '{exp.evidence}')",
                "experience", exp.years_min, exp.evidence,
            )
        return FilterDecision(False, None, None, exp.years_min, exp.evidence)


def _fmt_years(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:g}"
