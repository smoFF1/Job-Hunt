"""CV loader: extract text from registered CV PDFs, derive skills, cache by SHA-256.

- Unchanged file (same SHA-256) -> cached row is reused (no re-extraction).
- Changed file -> re-extracted automatically.
- Missing / unreadable / near-empty PDF -> event + warning, CV marked not-ok, scoring falls back
  to profile core_skills. Never raises for a bad CV.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from job_hunter.config import AppConfig, CVEntry
from job_hunter.core.db import log_event
from job_hunter.core.models import CVVersion, utcnow
from job_hunter.core.skills import SkillMatcher

log = logging.getLogger("job_hunter.cv_loader")

MIN_TEXT_CHARS = 200

Extractor = Callable[[Path], str]


@dataclass
class CVRecord:
    id: str
    file: str
    label: str
    tags: list[str]
    default: bool
    skills: list[str] = field(default_factory=list)
    ok: bool = False
    problem: str | None = None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_pdf_text(path: Path) -> str:
    """pdfplumber first, pypdf as fallback. Returns '' if both fail."""
    text = ""
    try:
        import pdfplumber

        with pdfplumber.open(path) as pdf:
            text = "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as exc:  # noqa: BLE001 - any parser failure falls through to pypdf
        log.debug("pdfplumber failed on %s: %s", path.name, exc)
    if len(text.strip()) >= MIN_TEXT_CHARS:
        return text
    try:
        from pypdf import PdfReader

        alt = "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)
        if len(alt.strip()) > len(text.strip()):
            text = alt
    except Exception as exc:  # noqa: BLE001
        log.debug("pypdf failed on %s: %s", path.name, exc)
    return text


def _quality_problem(text: str, skills: list[str]) -> str | None:
    if len(text.strip()) < MIN_TEXT_CHARS:
        return f"extracted text too short ({len(text.strip())} chars)"
    if not skills:
        return "no known skills found in extracted text"
    return None


def sync_cvs(
    session: Session,
    cfg: AppConfig,
    matcher: SkillMatcher,
    extractor: Extractor = extract_pdf_text,
    warnings: list[str] | None = None,
) -> list[CVRecord]:
    """Sync config/cvs.yaml into the cv_versions table and return usable CV records."""
    warnings = warnings if warnings is not None else []
    records: list[CVRecord] = []
    for entry in cfg.cvs:
        records.append(_sync_one(session, cfg, entry, matcher, extractor, warnings))
    session.flush()
    return records


def _sync_one(session: Session, cfg: AppConfig, entry: CVEntry, matcher: SkillMatcher,
              extractor: Extractor, warnings: list[str]) -> CVRecord:
    rec = CVRecord(entry.id, entry.file, entry.label, entry.tags, entry.default)
    path = Path(entry.file)
    if not path.is_absolute():
        path = cfg.root / path
    row = session.get(CVVersion, entry.id)

    def warn(problem: str) -> CVRecord:
        rec.ok, rec.problem = False, problem
        msg = f"CV '{entry.id}' ({entry.file}): {problem} -> falling back to profile core_skills"
        warnings.append(msg)
        log.warning(msg)
        log_event(session, "cv_extract_failed", msg)
        return rec

    if not path.exists():
        if row is None:
            row = CVVersion(id=entry.id, file=entry.file, sha256="", extracted_text="", skills_json=[],
                            added_at=utcnow(), is_default=entry.default)
            session.add(row)
        else:
            row.file, row.sha256, row.extracted_text, row.skills_json = entry.file, "", "", []
            row.is_default = entry.default
        return warn("file not found")

    try:
        digest = sha256_file(path)
    except OSError as exc:
        return warn(f"cannot read file ({exc.__class__.__name__})")

    if row is not None and row.sha256 == digest and row.file == entry.file:
        row.is_default = entry.default
        rec.skills = list(row.skills_json or [])
        problem = _quality_problem(row.extracted_text, rec.skills)
        log_event(session, "cv_cache_hit", f"{entry.id}: sha256 unchanged, {len(rec.skills)} skills")
        if problem:
            return warn(problem)
        rec.ok = True
        return rec

    try:
        text = extractor(path) or ""
    except Exception as exc:  # noqa: BLE001 - never crash on a bad CV
        text = ""
        log.debug("extractor raised for %s: %s", path.name, exc)
    skills = matcher.extract(text)

    action = "cv_loaded" if row is None else "cv_reextracted"
    if row is None:
        row = CVVersion(id=entry.id, added_at=utcnow())
        session.add(row)
    row.file, row.sha256, row.extracted_text, row.skills_json = entry.file, digest, text, skills
    row.is_default = entry.default
    log_event(session, action, f"{entry.id}: {len(text.strip())} chars, {len(skills)} skills")

    rec.skills = skills
    problem = _quality_problem(text, skills)
    if problem:
        return warn(problem)
    rec.ok = True
    return rec


def default_cv(records: list[CVRecord]) -> CVRecord | None:
    for r in records:
        if r.default:
            return r
    return records[0] if records else None


def suggest_cv(category_tag: str, records: list[CVRecord]) -> CVRecord | None:
    """CV whose tags include the category tag (default CV preferred on ties); else the default CV."""
    tag = (category_tag or "").lower()
    matches = [r for r in records if tag and tag in r.tags]
    if matches:
        for r in matches:
            if r.default:
                return r
        return matches[0]
    return default_cv(records)
