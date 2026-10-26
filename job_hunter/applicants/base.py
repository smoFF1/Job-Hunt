"""Applicator interface (implemented in Phase 4).

Hard rules: NEVER click a final submit button (Submit / Apply / Send / שלח / הגש מועמדות) — stop
one step before it. Only use fields from config/profile.yaml; unknown/custom fields stay empty and
the job becomes "Needs Input". Stop and flag on CAPTCHA/2FA/login wall/unexpected pages.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class FillResult:
    filled_fields: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)  # -> status "Needs Input"
    stopped_reason: str = ""


class BaseApplicator(ABC):
    @abstractmethod
    def detect_form(self, page) -> bool:
        """Return True if this applicator recognizes the application form on the page."""

    @abstractmethod
    def fill(self, page, profile: dict, cv_path: str) -> FillResult:
        """Fill known fields from the profile and attach the CV. Must not submit."""

    @abstractmethod
    def stop_before_submit(self, page) -> None:
        """Leave the browser on the final step for the human to review and submit."""
