"""Job category via ordered keyword rules from search.yaml (title first, then description)."""

from __future__ import annotations

from dataclasses import dataclass

from job_hunter.config import Category, SearchConfig
from job_hunter.core.text import KeywordMatcher

GENERAL = Category(name="General", tag="general", keywords=[])


@dataclass(frozen=True)
class CategoryMatch:
    category: Category
    where: str | None  # 'title' | 'description' | None (General fallback)
    matched: str | None


class Categorizer:
    def __init__(self, cfg: SearchConfig):
        self._rules = [(c, KeywordMatcher(c.keywords, cfg.hebrew_prefixes)) for c in cfg.categories]

    def categorize(self, title: str, description: str) -> CategoryMatch:
        for where, text in (("title", title), ("description", description)):
            for category, matcher in self._rules:
                hit = matcher.search(text)
                if hit:
                    return CategoryMatch(category, where, hit)
        return CategoryMatch(GENERAL, None, None)
