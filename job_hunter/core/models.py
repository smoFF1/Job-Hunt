"""ORM models (SQLAlchemy 2.x) — tables as defined in docs/SPEC.md "Data model".

Addition vs SPEC: jobs.url_normalized (needed for the normalized-URL UNIQUE constraint).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

STATUSES = (
    "New",
    "Skipped",
    "Shortlisted",
    "Needs Input",
    "Ready for Review",
    "Applied",
    "Rejected",
    "Interview",
    "Failed",
    "Expired",
)
# Statuses the engine may (re)assign. Anything else was set by the user/applicator and is never touched.
ENGINE_STATUSES = ("New", "Skipped", "Shortlisted")


def utcnow() -> datetime:
    """Naive UTC timestamp (SQLite has no timezone support)."""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class CVVersion(Base):
    __tablename__ = "cv_versions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    file: Mapped[str] = mapped_column(String, nullable=False)
    sha256: Mapped[str] = mapped_column(String, nullable=False, default="")
    extracted_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    skills_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    added_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_jobs_fingerprint"),
        UniqueConstraint("source", "source_job_id", name="uq_jobs_source_job"),
        UniqueConstraint("url_normalized", name="uq_jobs_url_normalized"),
        CheckConstraint("status IN (" + ", ".join(f"'{s}'" for s in STATUSES) + ")", name="ck_jobs_status"),
        CheckConstraint("match_score IS NULL OR (match_score BETWEEN 0 AND 100)", name="ck_jobs_score"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    source_job_id: Mapped[str | None] = mapped_column(String)
    url: Mapped[str | None] = mapped_column(Text)
    url_normalized: Mapped[str | None] = mapped_column(Text)
    apply_url: Mapped[str | None] = mapped_column(Text)
    company: Mapped[str] = mapped_column(String, nullable=False, default="")
    title: Mapped[str] = mapped_column(String, nullable=False)
    location: Mapped[str] = mapped_column(String, nullable=False, default="")
    description_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    language: Mapped[str | None] = mapped_column(String(8))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    years_required_min: Mapped[float | None] = mapped_column(Float)
    matched_skills: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    match_score: Mapped[int | None] = mapped_column(Integer)  # NULL = not processed yet
    cv_id: Mapped[str | None] = mapped_column(ForeignKey("cv_versions.id"))
    status: Mapped[str] = mapped_column(String, nullable=False, default="New")
    status_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"))
    action: Mapped[str] = mapped_column(String, nullable=False)
    result: Mapped[str] = mapped_column(Text, nullable=False, default="")


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    site: Mapped[str] = mapped_column(String, nullable=False)
    new_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shortlisted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
