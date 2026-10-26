"""0-100 match score (SPEC): role (30) + skills (30) + seniority (20) + domain (10) + recency (10).

All point values are configurable in search.yaml -> scoring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from job_hunter.config import SearchConfig
from job_hunter.core.categories import CategoryMatch
from job_hunter.core.skills import SkillMatcher
from job_hunter.core.text import KeywordMatcher


@dataclass
class ScoreBreakdown:
    role: int = 0
    skills: int = 0
    seniority: int = 0
    domain: int = 0
    recency: int = 0
    penalty: int = 0
    notes: dict[str, str] = field(default_factory=dict)

    @property
    def total(self) -> int:
        raw = self.role + self.skills + self.seniority + self.domain + self.recency + self.penalty
        return max(0, min(100, raw))

    def summary(self) -> str:
        base = (f"role {self.role}, skills {self.skills}, seniority {self.seniority}, "
                f"domain {self.domain}, recency {self.recency}")
        return f"{base}, penalty {self.penalty}" if self.penalty else base


class Scorer:
    def __init__(self, cfg: SearchConfig, skill_matcher: SkillMatcher):
        self.cfg = cfg
        self.sc = cfg.scoring
        p = cfg.hebrew_prefixes
        self.skill_matcher = skill_matcher
        self.generic_title = KeywordMatcher(cfg.generic_title_keywords, p)
        self.boost = KeywordMatcher(cfg.boost_keywords, p)
        self.domain = KeywordMatcher(cfg.domain_keywords, p)
        self.student = KeywordMatcher(cfg.student_keywords, p)
        self.graduate = KeywordMatcher(cfg.graduate_keywords, p)

    # -- components ----------------------------------------------------------------

    def role_points(self, title: str, cat: CategoryMatch) -> tuple[int, str]:
        if cat.where == "title":
            return self.sc.role_title_category, f"{cat.category.name} in title ('{cat.matched}')"
        hit = self.generic_title.search(title)
        if hit:
            return self.sc.role_generic_title, f"generic software title ('{hit}')"
        if cat.where == "description":
            return self.sc.role_description_category, f"{cat.category.name} in description ('{cat.matched}')"
        return 0, "no role match"

    def skill_points(self, job_skills: list[str], candidate: set[str]) -> tuple[int, list[str], str]:
        if not job_skills:
            return self.sc.skills_none_listed, [], "no known skills listed"
        matched = [s for s in job_skills if s in candidate]
        pts = round(self.sc.skills_max * len(matched) / len(job_skills))
        return pts, matched, f"{len(matched)}/{len(job_skills)} job skills matched"

    def seniority_points(self, text: str, years_min: float | None) -> tuple[int, str]:
        hit = self.boost.search(text)
        if hit:
            return self.sc.seniority_boost, f"boost keyword ('{hit}')"
        if years_min is not None and years_min <= self.sc.seniority_boost_max_years:
            return self.sc.seniority_boost, f"low experience requirement ({years_min:g} years)"
        return self.sc.seniority_neutral, "no seniority signal"

    def domain_points(self, text: str) -> tuple[int, str]:
        hit = self.domain.search(text)
        return (self.sc.domain_bonus, f"domain keyword ('{hit}')") if hit else (0, "no domain keyword")

    def recency_points(self, posted_at: datetime | None, now: datetime) -> tuple[int, str]:
        if posted_at is None:
            return self.sc.recency_unknown, "posting date unknown"
        days = max(0, (now - posted_at).days)
        for max_days, pts in self.sc.recency_buckets:
            if days <= max_days:
                return pts, f"posted {days} day(s) ago"
        return self.sc.recency_older, f"posted {days} day(s) ago"

    def penalty_points(self, title: str, description: str) -> tuple[int, str]:
        hit = self.student.search(description)
        if hit and not (self.graduate.search(title) or self.graduate.search(description)):
            pts = self.sc.student_description_penalty
            if pts:
                return pts, f"student keyword in description ('{hit}') without graduate"
        return 0, "no penalty"

    # -- total ---------------------------------------------------------------------

    def score(self, *, title: str, description: str, company: str, posted_at: datetime | None,
              category: CategoryMatch, years_min: float | None, candidate_skills: set[str],
              now: datetime) -> tuple[ScoreBreakdown, list[str], list[str]]:
        """Return (breakdown, job_skills, matched_skills)."""
        text = f"{title}\n{description}"
        b = ScoreBreakdown()
        b.role, b.notes["role"] = self.role_points(title, category)
        job_skills = self.skill_matcher.extract(text)
        b.skills, matched, b.notes["skills"] = self.skill_points(job_skills, candidate_skills)
        b.seniority, b.notes["seniority"] = self.seniority_points(text, years_min)
        b.domain, b.notes["domain"] = self.domain_points(f"{text}\n{company}")
        b.recency, b.notes["recency"] = self.recency_points(posted_at, now)
        b.penalty, b.notes["penalty"] = self.penalty_points(title, description)
        return b, job_skills, matched
