"""Database engine/session helpers and the single event-logging entry point."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from job_hunter import DEFAULT_DB_PATH, PROJECT_ROOT
from job_hunter.core.models import Base, Event


def resolve_db_path(db_path: str | Path | None = None) -> Path:
    """Explicit path > JOB_HUNTER_DB env var (.env) > data/job_hunter.db."""
    if db_path is None:
        load_dotenv(PROJECT_ROOT / ".env")
        env = os.environ.get("JOB_HUNTER_DB")
        db_path = env if env else DEFAULT_DB_PATH
    p = Path(db_path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p


def get_engine(db_path: str | Path | None = None) -> Engine:
    path = resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path.as_posix()}", future=True)

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


def init_db(engine: Engine) -> None:
    """Create all tables (idempotent)."""
    Base.metadata.create_all(engine)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def log_event(session: Session, action: str, result: str = "", job_id: int | None = None) -> Event:
    """Record an action in the events table (AGENTS.md rule 7). Never pass secrets here."""
    ev = Event(action=action, result=result, job_id=job_id)
    session.add(ev)
    return ev
