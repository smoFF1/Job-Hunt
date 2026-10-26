"""Skill extraction against the vocabulary in config/skills.yaml (no free-form NLP).

- Case-insensitive unless the skill is `case_sensitive` (e.g. "Go", "C", "REST").
- Symbol-aware boundaries: "C++", "C#", ".NET", "Node.js" match; "Java" doesn't match "JavaScript".
- Longest alias wins at a position: "React Native" does not also count as "React".
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from job_hunter.config import SkillDef
from job_hunter.core.text import normalize_for_match


def _key(term: str) -> str:
    return " ".join(term.lower().replace("-", " ").split())


def _alias_pattern(alias: str, case_sensitive: bool) -> str:
    piece = re.escape(normalize_for_match(alias))
    piece = re.sub(r"(?:\\ |\\-|-| )+", r"[\\s\\-]+", piece)
    return f"(?-i:{piece})" if case_sensitive else piece


class SkillMatcher:
    def __init__(self, skills: Iterable[SkillDef]):
        self._canonical: dict[str, str] = {}
        terms: list[tuple[str, bool]] = []
        for s in skills:
            for term in [s.name, *s.aliases]:
                self._canonical[_key(term)] = s.name
                terms.append((term, s.case_sensitive))
        terms.sort(key=lambda t: len(t[0]), reverse=True)
        if terms:
            alternation = "|".join(_alias_pattern(t, cs) for t, cs in terms)
            self._re = re.compile(rf"(?<![A-Za-z0-9_])(?:{alternation})(?![A-Za-z0-9_+#])", re.IGNORECASE)
        else:
            self._re = None

    def canonical(self, term: str) -> str | None:
        """Map a skill name or alias to its canonical name (None if unknown)."""
        return self._canonical.get(_key(term))

    def extract(self, text: str | None) -> list[str]:
        """Canonical skills found in text, unique, in order of first appearance."""
        if not text or self._re is None:
            return []
        found: list[str] = []
        for m in self._re.finditer(normalize_for_match(text)):
            name = self._canonical.get(_key(m.group(0)))
            if name and name not in found:
                found.append(name)
        return found

    def canonicalize_all(self, terms: Iterable[str]) -> list[str]:
        """Map names to canonical form; unknown terms are kept as-is."""
        out: list[str] = []
        for t in terms:
            name = self.canonical(t) or t
            if name not in out:
                out.append(name)
        return out
