"""On-demand multi-site scan runner, single-scan lock, cooldown/block lockout, and subprocess manager.

Design invariants (AGENTS.md & Phase 4 spec):
- Scans NEVER run on a schedule or in the background on their own; only when explicitly triggered.
- Single-scan lock with stale-PID recovery on dashboard startup and scan start.
- Per-site cooldown (`min_minutes_between_scans`, default 30) applies to BOTH normal and dry-run scans.
- Per-site block lockout (`min_minutes_after_block`, default 240) applies after CAPTCHA/block/robots failure.
- Fault isolation: a block/CAPTCHA/robots error or crash on one site stops THAT site only and continues.
- Dry-run writes zero rows to SQLite (`jobs`, `runs`, `events`, `cv_versions`), recording progress and
  site request timestamps strictly in `<db>.scan_state.json`.
"""

from __future__ import annotations

import ctypes
import json
import logging
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from job_hunter import PROJECT_ROOT
from job_hunter.config import (
    DEFAULT_BLOCK_LOCKOUT_MINUTES,
    DEFAULT_COOLDOWN_MINUTES,
    AppConfig,
)
from job_hunter.core.db import log_event, resolve_db_path, session_scope
from job_hunter.core.models import Event, Run, utcnow
from job_hunter.core.skills import SkillMatcher
from job_hunter.scrapers.http import ScanStoppedError
from job_hunter.scrapers.registry import registered_sites
from job_hunter.scrapers.runner import ScrapeSummary, SiteProgress, scrape_site

log = logging.getLogger("job_hunter.scrapers.scan")

MAX_EXTRA_QUERY_LEN = 100
MAX_SUMMARY_NEW_JOBS = 40
_SITE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_FORBIDDEN_QUERY_CHARS = frozenset(";|&`$<>\\/")
_URL_PREFIXES = ("http:", "https:", "javascript:", "file:", "ftp:", "data:", "www.")


class ScanError(RuntimeError):
    """Base error for on-demand scan management."""


class ScanValidationError(ScanError):
    """Invalid site selection or unsafe extra query."""


class ScanAlreadyRunningError(ScanError):
    """Another scan holds the single-scan lock."""


class ScanCooldownError(ScanError):
    """One or more requested sites are within cooldown or block lockout."""


# --------------------------------------------------------------------------- File paths & time helpers


def scan_lock_path(db_path: str | Path | None) -> Path:
    resolved = resolve_db_path(db_path)
    return resolved.with_name(f"{resolved.name}.scan.lock")


def scan_state_path(db_path: str | Path | None) -> Path:
    resolved = resolve_db_path(db_path)
    return resolved.with_name(f"{resolved.name}.scan_state.json")


def scan_stop_path(db_path: str | Path | None) -> Path:
    resolved = resolve_db_path(db_path)
    return resolved.with_name(f"{resolved.name}.scan.stop")


def _dt_to_iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="microseconds") if dt is not None else None


def _iso_to_dt(raw: str | None) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
        return parsed.replace(tzinfo=None)
    except ValueError:
        return None


# --------------------------------------------------------------------------- Process liveness & termination


def is_pid_alive(pid: int | None) -> bool:
    """Return True iff OS process `pid` is currently alive."""
    if pid is None or pid <= 0:
        return False
    if pid == os.getpid():
        return True

    if sys.platform == "win32":
        kernel32 = ctypes.windll.kernel32
        process_query_limited_information = 0x1000
        still_active = 259
        error_access_denied = 5

        handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
        if not handle:
            return int(kernel32.GetLastError()) == error_access_denied
        try:
            exit_code = ctypes.c_ulong(0)
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            if not ok:
                return False
            return int(exit_code.value) == still_active
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def terminate_pid(pid: int | None) -> None:
    """Terminate an external scan process by PID when not directly owned via `Popen`."""
    if pid is None or pid <= 0 or pid == os.getpid():
        return
    if sys.platform == "win32":
        kernel32 = ctypes.windll.kernel32
        process_terminate = 0x0001
        handle = kernel32.OpenProcess(process_terminate, False, int(pid))
        if handle:
            try:
                kernel32.TerminateProcess(handle, 1)
            finally:
                kernel32.CloseHandle(handle)
        return
    try:
        os.kill(int(pid), signal.SIGTERM)
    except OSError:
        pass


# --------------------------------------------------------------------------- JSON state & lock persistence


def default_state_dict() -> dict[str, Any]:
    return {
        "scan_id": None,
        "state": "idle",  # idle | running | completed | stopped | failed
        "dry_run": False,
        "pid": None,
        "started_at": None,
        "finished_at": None,
        "sites_requested": [],
        "extra_query": None,
        "current_site": None,
        "current_site_index": 0,
        "total_sites": 0,
        "query_index": 0,
        "queries_total": 0,
        "current_query": None,
        "pages_fetched": 0,
        "jobs_found_so_far": 0,
        "error": None,
        "site_results": [],
        "site_history": {},
    }


def read_scan_state(db_path: str | Path | None) -> dict[str, Any]:
    path = scan_state_path(db_path)
    base = default_state_dict()
    if not path.exists():
        return base
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            base.update(raw)
            if not isinstance(base.get("site_history"), dict):
                base["site_history"] = {}
            if not isinstance(base.get("site_results"), list):
                base["site_results"] = []
    except (OSError, json.JSONDecodeError):
        pass
    return base


def write_scan_state(db_path: str | Path | None, data: dict[str, Any]) -> None:
    path = scan_state_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.02)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def update_site_history(
    db_path: str | Path | None,
    site: str,
    *,
    last_request_at: datetime | None = None,
    last_blocked_at: datetime | None = None,
    last_block_reason: str | None = None,
    clear_block: bool = False,
) -> dict[str, Any]:
    state = read_scan_state(db_path)
    hist = dict(state.get("site_history") or {})
    entry = dict(hist.get(site) or {})
    if last_request_at is not None:
        entry["last_request_at"] = _dt_to_iso(last_request_at)
    if clear_block:
        entry["last_blocked_at"] = None
        entry["last_block_reason"] = None
    if last_blocked_at is not None:
        entry["last_blocked_at"] = _dt_to_iso(last_blocked_at)
        entry["last_block_reason"] = last_block_reason or "Blocked by site"
    hist[site] = entry
    state["site_history"] = hist
    write_scan_state(db_path, state)
    return state


def read_lock_info(db_path: str | Path | None) -> dict[str, Any] | None:
    path = scan_lock_path(db_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"pid": None}
    except (OSError, json.JSONDecodeError):
        return {"pid": None}


def _write_exclusive_lock(lock_path: Path, payload: dict[str, Any]) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(str(lock_path), flags, 0o600)
    try:
        os.write(fd, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    finally:
        os.close(fd)


def _replace_lock_file(lock_path: Path, payload: dict[str, Any]) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = lock_path.with_name(f"{lock_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        for attempt in range(5):
            try:
                os.replace(tmp, lock_path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.02)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def release_lock_and_stop_files(
    db_path: str | Path | None,
    *,
    expected_pid: int | None = None,
    expected_scan_id: str | None = None,
) -> None:
    lock_path = scan_lock_path(db_path)
    if lock_path.exists():
        if expected_pid is not None:
            info = read_lock_info(db_path)
            same_owner = bool(
                info is None
                or info.get("pid") in (None, expected_pid)
                or (expected_scan_id and info.get("scan_id") == expected_scan_id)
            )
            if not same_owner and info and is_pid_alive(info.get("pid")):
                return
        try:
            lock_path.unlink()
        except OSError:
            pass
    stop_path = scan_stop_path(db_path)
    if stop_path.exists():
        try:
            stop_path.unlink()
        except OSError:
            pass


def cleanup_stale_scan(
    db_path: str | Path | None,
    factory: sessionmaker[Session] | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    """Clean up dead-PID lock, stale `running` state file, and orphaned `runs` rows.

    Returns True if any stale state was cleaned; False if idle or an active PID is running.
    """
    ref_now = now or utcnow()
    lock_info = read_lock_info(db_path)
    state = read_scan_state(db_path)

    active_pid: int | None = None
    if lock_info:
        for key in ("pid", "launcher_pid"):
            cand = lock_info.get(key)
            if isinstance(cand, int) and is_pid_alive(cand):
                active_pid = cand
                break
    if active_pid is None and state.get("state") == "running":
        for key in ("pid", "launcher_pid"):
            cand = state.get(key)
            if isinstance(cand, int) and is_pid_alive(cand):
                active_pid = cand
                break

    if active_pid is not None:
        return False

    cleaned = False
    if lock_info is not None:
        release_lock_and_stop_files(db_path)
        cleaned = True

    was_dry_run = bool(state.get("dry_run", False))
    if state.get("state") == "running":
        state["state"] = "failed"
        state["finished_at"] = _dt_to_iso(ref_now)
        state["current_site"] = None
        state["error"] = "Interrupted: stale running state cleaned up (process died)"
        for sr in state.get("site_results", []):
            if isinstance(sr, dict) and sr.get("status") in ("running", "pending"):
                sr["status"] = "error"
                errs = list(sr.get("errors") or [])
                errs.append("Process died before completion")
                sr["errors"] = errs
        write_scan_state(db_path, state)
        cleaned = True

    resolved_db = resolve_db_path(db_path)
    if factory is not None and not was_dry_run and resolved_db.exists():
        with session_scope(factory) as s:
            orphans = list(s.scalars(select(Run).where(Run.finished_at.is_(None))).all())
            for r in orphans:
                r.finished_at = ref_now
                r.errors = [*list(r.errors or []), "Interrupted: stale running state cleaned up (process died)"]
                log_event(s, "scan_stale_cleaned", f"cleaned stale run #{r.id} ({r.site})")
                cleaned = True

    return cleaned


def acquire_scan_lock(
    db_path: str | Path | None,
    *,
    pid: int,
    scan_id: str,
    adopt_existing: bool = False,
    factory: sessionmaker[Session] | None = None,
    now: datetime | None = None,
) -> None:
    """Acquire `.scan.lock` for `pid` (adopting a parent-created lock when `adopt_existing=True`)."""
    ref_now = now or utcnow()
    lock_path = scan_lock_path(db_path)
    payload: dict[str, Any] = {"pid": pid, "scan_id": scan_id, "started_at": _dt_to_iso(ref_now)}

    for attempt in range(2):
        try:
            _write_exclusive_lock(lock_path, payload)
            return
        except FileExistsError as exc:
            info = read_lock_info(db_path) or {}
            holder_pid = info.get("pid")
            can_adopt = (isinstance(holder_pid, int) and holder_pid == pid) or (
                adopt_existing and bool(scan_id) and info.get("scan_id") == scan_id
            )
            if can_adopt:
                adopted_payload: dict[str, Any] = {
                    "pid": pid,
                    "launcher_pid": info.get("launcher_pid") or holder_pid,
                    "scan_id": scan_id,
                    "started_at": info.get("started_at") or _dt_to_iso(ref_now),
                }
                _replace_lock_file(lock_path, adopted_payload)
                return
            if isinstance(holder_pid, int) and is_pid_alive(holder_pid):
                raise ScanAlreadyRunningError(f"A scan is already running (PID {holder_pid}).") from exc
            launcher_pid = info.get("launcher_pid")
            if isinstance(launcher_pid, int) and is_pid_alive(launcher_pid):
                raise ScanAlreadyRunningError(f"A scan is already running (PID {launcher_pid}).") from exc
            if attempt == 0:
                cleanup_stale_scan(db_path, factory, now=ref_now)
                continue
            raise ScanAlreadyRunningError("Unable to acquire scan lock.") from exc


# --------------------------------------------------------------------------- Site registry, validation & cooldown


@dataclass(frozen=True)
class SiteScanStatus:
    name: str
    enabled: bool
    last_scan_at: datetime | None
    min_minutes_between_scans: float
    min_minutes_after_block: float
    on_cooldown: bool = False
    blocked_lockout: bool = False
    cooldown_minutes: int = 0
    cooldown_label: str = ""
    block_reason: str | None = None

    @property
    def selectable(self) -> bool:
        return self.enabled and not self.on_cooldown and not self.blocked_lockout


def site_cooldown_minutes(site_cfg: dict[str, Any] | None) -> float:
    raw = (site_cfg or {}).get("min_minutes_between_scans")
    return float(DEFAULT_COOLDOWN_MINUTES if raw is None else raw)


def site_block_lockout_minutes(site_cfg: dict[str, Any] | None) -> float:
    raw = (site_cfg or {}).get("min_minutes_after_block")
    return float(DEFAULT_BLOCK_LOCKOUT_MINUTES if raw is None else raw)


def validate_scan_request(
    sites: Sequence[str],
    extra_query: str | None,
    cfg: AppConfig,
) -> tuple[list[str], str | None]:
    """Validate requested site names against registry+config and sanitize optional `extra_query`."""
    known = set(registered_sites())
    clean_sites: list[str] = []
    seen: set[str] = set()
    for raw in sites:
        s = (raw or "").strip()
        if not s:
            continue
        if not _SITE_NAME_RE.match(s) or s not in known:
            raise ScanValidationError(f"Unknown or unregistered site: {s!r}")
        site_cfg = cfg.search.sites.get(s)
        if not isinstance(site_cfg, dict) or site_cfg.get("enabled") is not True:
            raise ScanValidationError(f"Site '{s}' is disabled in config/search.yaml")
        if s not in seen:
            seen.add(s)
            clean_sites.append(s)

    if not clean_sites:
        raise ScanValidationError("Select at least one enabled site to scan.")

    q = (extra_query or "").strip()
    if not q:
        return clean_sites, None

    if len(q) > MAX_EXTRA_QUERY_LEN:
        raise ScanValidationError(f"Extra query must be <= {MAX_EXTRA_QUERY_LEN} characters.")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in q):
        raise ScanValidationError("Extra query contains invalid control characters.")
    q_low = q.lower()
    if "://" in q_low or q_low.startswith(_URL_PREFIXES) or any(ch in _FORBIDDEN_QUERY_CHARS for ch in q):
        raise ScanValidationError("Extra query must be a plain search term (URLs, paths, and commands are forbidden).")
    if q.startswith("-"):
        raise ScanValidationError("Extra query must not start with '-'.")

    return clean_sites, q


def get_site_scan_statuses(
    session: Session | None,
    cfg: AppConfig,
    db_path: str | Path | None,
    *,
    now: datetime | None = None,
) -> list[SiteScanStatus]:
    """Build ordered `SiteScanStatus` entries dynamically from scraper registry + `search.yaml`."""
    ref_now = now or utcnow()
    state = read_scan_state(db_path)
    hist_map: dict[str, Any] = state.get("site_history") or {}

    db_last_by_site: dict[str, datetime] = {}
    if session is not None:
        for name in registered_sites():
            latest_run = session.scalars(
                select(Run)
                .where(Run.site == name)
                .order_by(Run.started_at.desc(), Run.id.desc())
                .limit(1)
            ).first()
            if latest_run is not None:
                db_last_by_site[name] = latest_run.finished_at or latest_run.started_at

    statuses: list[SiteScanStatus] = []
    for name in registered_sites():
        site_cfg = cfg.search.sites.get(name) or {}
        enabled = site_cfg.get("enabled") is True
        min_between = site_cooldown_minutes(site_cfg)
        min_after_block = site_block_lockout_minutes(site_cfg)

        hist = hist_map.get(name) if isinstance(hist_map.get(name), dict) else {}
        hist_last = _iso_to_dt(hist.get("last_request_at"))
        db_last = db_last_by_site.get(name)
        if hist_last and db_last:
            last_scan_at: datetime | None = max(hist_last, db_last)
        else:
            last_scan_at = hist_last or db_last

        last_blocked_at = _iso_to_dt(hist.get("last_blocked_at"))
        block_reason = hist.get("last_block_reason") if isinstance(hist.get("last_block_reason"), str) else None

        block_rem_sec = 0.0
        if last_blocked_at is not None and min_after_block > 0:
            block_until = last_blocked_at + timedelta(minutes=min_after_block)
            block_rem_sec = max(0.0, (block_until - ref_now).total_seconds())

        normal_rem_sec = 0.0
        if last_scan_at is not None and min_between > 0:
            normal_until = last_scan_at + timedelta(minutes=min_between)
            normal_rem_sec = max(0.0, (normal_until - ref_now).total_seconds())

        if block_rem_sec > 0:
            eff_sec = max(block_rem_sec, normal_rem_sec)
            mins = max(1, math.ceil(eff_sec / 60.0))
            statuses.append(
                SiteScanStatus(
                    name=name,
                    enabled=enabled,
                    last_scan_at=last_scan_at,
                    min_minutes_between_scans=min_between,
                    min_minutes_after_block=min_after_block,
                    on_cooldown=True,
                    blocked_lockout=True,
                    cooldown_minutes=mins,
                    cooldown_label=f"blocked — available in {mins} min",
                    block_reason=block_reason or "Blocked / challenge encountered",
                )
            )
        elif normal_rem_sec > 0:
            mins = max(1, math.ceil(normal_rem_sec / 60.0))
            statuses.append(
                SiteScanStatus(
                    name=name,
                    enabled=enabled,
                    last_scan_at=last_scan_at,
                    min_minutes_between_scans=min_between,
                    min_minutes_after_block=min_after_block,
                    on_cooldown=True,
                    blocked_lockout=False,
                    cooldown_minutes=mins,
                    cooldown_label=f"available in {mins} min",
                    block_reason=None,
                )
            )
        else:
            statuses.append(
                SiteScanStatus(
                    name=name,
                    enabled=enabled,
                    last_scan_at=last_scan_at,
                    min_minutes_between_scans=min_between,
                    min_minutes_after_block=min_after_block,
                )
            )

    return statuses


def assert_sites_not_on_cooldown(
    sites: Sequence[str],
    statuses: Sequence[SiteScanStatus],
) -> None:
    by_name = {st.name: st for st in statuses}
    for s in sites:
        st = by_name.get(s)
        if st is None:
            continue
        if st.blocked_lockout:
            detail = f" ({st.block_reason})" if st.block_reason else ""
            raise ScanCooldownError(f"Site '{s}' is {st.cooldown_label}{detail}.")
        if st.on_cooldown:
            raise ScanCooldownError(f"Site '{s}' is on cooldown ({st.cooldown_label}).")


# --------------------------------------------------------------------------- CLI `scan` execution


@dataclass
class ScanRunResult:
    scan_id: str
    dry_run: bool
    stopped: bool = False
    summaries: list[ScrapeSummary] = field(default_factory=list)
    site_errors: dict[str, str] = field(default_factory=dict)


def _summary_new_jobs_payload(summary: ScrapeSummary) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for p in summary.previews:
        if p.duplicate_of_id is not None or p.evaluation is None:
            continue
        items.append(
            {
                "id": p.job_id,
                "title": p.posting.title,
                "company": p.posting.company,
                "status": p.evaluation.status,
                "score": p.evaluation.score,
                "url": p.posting.url,
            }
        )
        if len(items) >= MAX_SUMMARY_NEW_JOBS:
            break
    return items


def _make_interruptible_sleep(
    should_stop: Callable[[], bool],
    base_sleep: Callable[[float], None] = time.sleep,
) -> Callable[[float], None]:
    def _sleep(seconds: float) -> None:
        remaining = max(0.0, float(seconds))
        step = 0.1
        while remaining > 0:
            if should_stop():
                raise ScanStoppedError("Stopped by user")
            chunk = min(step, remaining)
            base_sleep(chunk)
            remaining -= chunk
        if should_stop():
            raise ScanStoppedError("Stopped by user")

    return _sleep


def run_scan(
    factory: sessionmaker[Session],
    cfg: AppConfig,
    matcher: SkillMatcher,
    *,
    db_path: str | Path | None,
    sites: Sequence[str],
    extra_query: str | None = None,
    dry_run: bool = False,
    scan_id: str | None = None,
    transport_by_site: dict[str, Any] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    now: datetime | None = None,
) -> ScanRunResult:
    """Execute a multi-site scan sequentially, updating `.scan_state.json` and enforcing lock/cooldown."""
    clean_sites, clean_query = validate_scan_request(sites, extra_query, cfg)
    sid = scan_id or uuid.uuid4().hex[:12]
    my_pid = os.getpid()
    ref_start = now or utcnow()

    acquire_scan_lock(
        db_path,
        pid=my_pid,
        scan_id=sid,
        adopt_existing=bool(scan_id),
        factory=None if dry_run else factory,
        now=ref_start,
    )

    stop_file = scan_stop_path(db_path)
    stop_signal_received = False

    def _sig_handler(_signum: int, _frame: Any) -> None:
        nonlocal stop_signal_received
        stop_signal_received = True

    def _should_stop() -> bool:
        return stop_signal_received or stop_file.exists()

    prev_sigint = None
    prev_sigterm = None
    if threading.current_thread() is threading.main_thread():
        prev_sigint = signal.getsignal(signal.SIGINT)
        prev_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGINT, _sig_handler)
        signal.signal(signal.SIGTERM, _sig_handler)

    try:
        # Enforce per-site cooldown & block lockout (unless already pre-checked for this exact scan_id).
        existing_state = read_scan_state(db_path)
        prechecked_by_parent = (
            bool(scan_id)
            and existing_state.get("scan_id") == sid
            and existing_state.get("state") == "running"
        )
        if not prechecked_by_parent:
            if dry_run:
                s = factory()
                try:
                    statuses = get_site_scan_statuses(s, cfg, db_path, now=ref_start)
                    s.rollback()
                finally:
                    s.close()
            else:
                with session_scope(factory) as s:
                    statuses = get_site_scan_statuses(s, cfg, db_path, now=ref_start)
            assert_sites_not_on_cooldown(clean_sites, statuses)

        # Initialize scan state while preserving `site_history` across runs.
        site_history = dict(existing_state.get("site_history") or {})
        launcher_pid = existing_state.get("launcher_pid")
        active_queries = [clean_query] if clean_query else list(cfg.search.roles)

        site_results: list[dict[str, Any]] = [
            {
                "site": s_name,
                "status": "pending",
                "run_id": None,
                "pages_fetched": 0,
                "queries_run": 0,
                "queries_planned": len(active_queries),
                "unique_postings": 0,
                "new_count": 0,
                "skipped_count": 0,
                "shortlisted_count": 0,
                "duplicate_count": 0,
                "errors": [],
                "abort_reason": None,
                "new_jobs": [],
            }
            for s_name in clean_sites
        ]

        state: dict[str, Any] = {
            "scan_id": sid,
            "state": "running",
            "dry_run": dry_run,
            "pid": my_pid,
            "launcher_pid": launcher_pid,
            "started_at": _dt_to_iso(ref_start),
            "finished_at": None,
            "sites_requested": list(clean_sites),
            "extra_query": clean_query,
            "current_site": clean_sites[0],
            "current_site_index": 1,
            "total_sites": len(clean_sites),
            "query_index": 0,
            "queries_total": len(active_queries),
            "current_query": None,
            "pages_fetched": 0,
            "jobs_found_so_far": 0,
            "error": None,
            "site_results": site_results,
            "site_history": site_history,
        }
        write_scan_state(db_path, state)

        if not dry_run:
            with session_scope(factory) as s:
                log_event(
                    s,
                    "scan_started",
                    f"scan_id={sid} sites={','.join(clean_sites)} "
                    f"query={clean_query or '<roles>'} start={_dt_to_iso(ref_start)}",
                )

        effective_sleep = _make_interruptible_sleep(_should_stop, sleep_fn or time.sleep)
        result = ScanRunResult(scan_id=sid, dry_run=dry_run)
        completed_pages = 0
        completed_jobs = 0

        for idx, site in enumerate(clean_sites):
            if _should_stop():
                result.stopped = True
                site_results[idx]["status"] = "stopped"
                site_results[idx]["abort_reason"] = "Stopped by user"
                break

            site_now = now or utcnow()
            site_results[idx]["status"] = "running"
            state["current_site"] = site
            state["current_site_index"] = idx + 1
            state["query_index"] = 0
            state["queries_total"] = len(active_queries)
            state["current_query"] = None

            # Record request start timestamp in site_history even for dry-run scans!
            hist_entry = dict(site_history.get(site) or {})
            hist_entry["last_request_at"] = _dt_to_iso(site_now)
            site_history[site] = hist_entry
            state["site_history"] = site_history
            write_scan_state(db_path, state)

            def _on_site_progress(
                prog: SiteProgress,
                _idx: int = idx,
                _base_pages: int = completed_pages,
                _base_jobs: int = completed_jobs,
            ) -> None:
                site_results[_idx]["pages_fetched"] = prog.pages_fetched
                site_results[_idx]["queries_run"] = prog.query_index
                site_results[_idx]["queries_planned"] = prog.queries_total
                site_results[_idx]["unique_postings"] = prog.jobs_found
                state["query_index"] = prog.query_index
                state["queries_total"] = prog.queries_total
                state["current_query"] = prog.current_query
                state["pages_fetched"] = _base_pages + prog.pages_fetched
                state["jobs_found_so_far"] = _base_jobs + prog.jobs_found
                write_scan_state(db_path, state)

            site_transport = (transport_by_site or {}).get(site)
            try:
                summary = scrape_site(
                    factory,
                    cfg,
                    matcher,
                    site,
                    dry_run=dry_run,
                    queries=[clean_query] if clean_query else None,
                    transport=site_transport,
                    sleep_fn=effective_sleep,
                    now=site_now,
                    on_progress=_on_site_progress,
                    should_stop=_should_stop,
                )
            except Exception as exc:  # noqa: BLE001 - isolate site crash; continue with next site
                err_msg = f"{exc.__class__.__name__}: {exc}"
                log.error("site %s crashed during scan: %s", site, err_msg)
                result.site_errors[site] = err_msg
                site_results[idx]["status"] = "error"
                site_results[idx]["errors"] = [err_msg]
                site_results[idx]["abort_reason"] = err_msg
                hist_entry = dict(site_history.get(site) or {})
                hist_entry["last_request_at"] = _dt_to_iso(now or utcnow())
                site_history[site] = hist_entry
                state["site_history"] = site_history
                write_scan_state(db_path, state)
                if not dry_run:
                    with session_scope(factory) as s:
                        log_event(s, "scan_site_error", f"scan_id={sid} site={site} {err_msg}")
                continue

            result.summaries.append(summary)
            finish_site_dt = now or utcnow()
            hist_entry = dict(site_history.get(site) or {})
            hist_entry["last_request_at"] = _dt_to_iso(finish_site_dt)
            if summary.blocked:
                hist_entry["last_blocked_at"] = _dt_to_iso(finish_site_dt)
                hist_entry["last_block_reason"] = summary.abort_reason or "Blocked by site"
            site_history[site] = hist_entry
            state["site_history"] = site_history

            completed_pages += summary.pages_fetched
            completed_jobs += summary.unique_postings
            state["pages_fetched"] = completed_pages
            state["jobs_found_so_far"] = completed_jobs

            sr = site_results[idx]
            sr["run_id"] = summary.run_id
            sr["pages_fetched"] = summary.pages_fetched
            sr["queries_run"] = summary.queries_run
            sr["queries_planned"] = summary.queries_planned
            sr["unique_postings"] = summary.unique_postings
            sr["new_count"] = summary.new_count
            sr["skipped_count"] = summary.skipped_count
            sr["shortlisted_count"] = summary.shortlisted_count
            sr["duplicate_count"] = summary.duplicate_count
            sr["errors"] = list(summary.errors)
            sr["abort_reason"] = summary.abort_reason
            sr["new_jobs"] = _summary_new_jobs_payload(summary)

            if summary.stopped:
                sr["status"] = "stopped"
                result.stopped = True
                write_scan_state(db_path, state)
                break
            if summary.blocked:
                sr["status"] = "blocked"
            elif summary.errors:
                sr["status"] = "completed"
            else:
                sr["status"] = "completed"

            if not dry_run and summary.run_id is not None:
                with session_scope(factory) as s:
                    log_event(
                        s,
                        "scan_site_done",
                        f"scan_id={sid} start={_dt_to_iso(ref_start)} "
                        f"run_id={summary.run_id} site={site} new={summary.new_count}",
                    )

            write_scan_state(db_path, state)

        end_dt = now or utcnow()
        state["state"] = "stopped" if result.stopped else "completed"
        state["finished_at"] = _dt_to_iso(end_dt)
        state["current_site"] = None
        state["current_query"] = None
        write_scan_state(db_path, state)

        if not dry_run:
            total_new = sum(sm.new_count for sm in result.summaries)
            action = "scan_stopped" if result.stopped else "scan_finished"
            with session_scope(factory) as s:
                log_event(
                    s,
                    action,
                    f"scan_id={sid} sites={','.join(clean_sites)} new={total_new} stopped={result.stopped}",
                )

        return result
    except BaseException as exc:
        cur = read_scan_state(db_path)
        if cur.get("scan_id") == sid and cur.get("state") == "running":
            cur["state"] = "failed"
            cur["finished_at"] = _dt_to_iso(now or utcnow())
            cur["current_site"] = None
            cur["error"] = str(exc)
            write_scan_state(db_path, cur)
        raise
    finally:
        if threading.current_thread() is threading.main_thread():
            if prev_sigint is not None:
                signal.signal(signal.SIGINT, prev_sigint)
            if prev_sigterm is not None:
                signal.signal(signal.SIGTERM, prev_sigterm)
        release_lock_and_stop_files(db_path, expected_pid=my_pid, expected_scan_id=sid)


# --------------------------------------------------------------------------- Dashboard Subprocess Manager


class ScanManager:
    """Dashboard-side controller that spawns `python -m job_hunter scan` in a separate process."""

    def __init__(
        self,
        *,
        db_path: Path,
        config_dir: Path,
        root: Path,
        session_factory: sessionmaker[Session],
        cfg: AppConfig,
    ):
        self.db_path = Path(db_path)
        self.config_dir = Path(config_dir)
        self.root = Path(root)
        self.session_factory = session_factory
        self.cfg = cfg
        self._mutex = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None

    def _reap_unlocked(self) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is not None:
            self._proc = None
            return
        # On Windows `.venv`, the launcher stub `.venv\Scripts\python.exe` may take a few milliseconds
        # to exit after the real Python interpreter has already completed and released `.scan.lock`.
        lock = read_lock_info(self.db_path)
        st = read_scan_state(self.db_path).get("state")
        if lock is None and st in ("idle", "completed", "stopped", "failed"):
            try:
                self._proc.wait(timeout=1.0)
                self._proc = None
            except subprocess.TimeoutExpired:
                pass

    def get_status(self) -> dict[str, Any]:
        """Return up-to-date scan state dictionary, cleaning up if the worker died unexpectedly."""
        with self._mutex:
            self._reap_unlocked()
            state = read_scan_state(self.db_path)
            if state.get("state") == "running":
                pid = state.get("pid")
                launcher_pid = state.get("launcher_pid")
                lock = read_lock_info(self.db_path)
                lock_pid = lock.get("pid") if lock else None
                lock_launcher = lock.get("launcher_pid") if lock else None
                alive = any(
                    isinstance(cand, int) and is_pid_alive(cand)
                    for cand in (pid, launcher_pid, lock_pid, lock_launcher)
                )
                if not alive:
                    cleanup_stale_scan(self.db_path, self.session_factory)
                    state = read_scan_state(self.db_path)
            return state

    def is_running(self) -> bool:
        return self.get_status().get("state") == "running"

    def start_scan(
        self,
        *,
        sites: Sequence[str],
        extra_query: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Validate, acquire single-scan lock, and launch `python -m job_hunter scan` subprocess."""
        with self._mutex:
            self._reap_unlocked()

            lock = read_lock_info(self.db_path)
            if lock:
                for key in ("pid", "launcher_pid"):
                    cand = lock.get(key)
                    if isinstance(cand, int) and is_pid_alive(cand):
                        raise ScanAlreadyRunningError(f"A scan is already running (PID {cand}).")
            if self._proc is not None and self._proc.poll() is None:
                raise ScanAlreadyRunningError(f"A scan is already running (PID {self._proc.pid}).")

            cleanup_stale_scan(self.db_path, self.session_factory)

            clean_sites, clean_query = validate_scan_request(sites, extra_query, self.cfg)

            s = self.session_factory()
            try:
                statuses = get_site_scan_statuses(s, self.cfg, self.db_path)
                s.rollback()
            finally:
                s.close()
            assert_sites_not_on_cooldown(clean_sites, statuses)

            stop_file = scan_stop_path(self.db_path)
            if stop_file.exists():
                try:
                    stop_file.unlink()
                except OSError:
                    pass

            scan_id = uuid.uuid4().hex[:12]
            now_dt = utcnow()
            prev_state = read_scan_state(self.db_path)
            site_history = dict(prev_state.get("site_history") or {})
            planned_queries = 1 if clean_query else len(self.cfg.search.roles)

            # Pre-acquire `.scan.lock` and initial state BEFORE `subprocess.Popen` so there is zero
            # race window either for rapid double-POSTs or for the child reading `existing_state`.
            acquire_scan_lock(self.db_path, pid=os.getpid(), scan_id=scan_id, now=now_dt)

            initial_state: dict[str, Any] = {
                "scan_id": scan_id,
                "state": "running",
                "dry_run": dry_run,
                "pid": None,
                "launcher_pid": None,
                "started_at": _dt_to_iso(now_dt),
                "finished_at": None,
                "sites_requested": list(clean_sites),
                "extra_query": clean_query,
                "current_site": clean_sites[0],
                "current_site_index": 1,
                "total_sites": len(clean_sites),
                "query_index": 0,
                "queries_total": planned_queries,
                "current_query": clean_query,
                "pages_fetched": 0,
                "jobs_found_so_far": 0,
                "error": None,
                "site_results": [
                    {
                        "site": s_name,
                        "status": "running" if i == 0 else "pending",
                        "run_id": None,
                        "pages_fetched": 0,
                        "queries_run": 0,
                        "queries_planned": planned_queries,
                        "unique_postings": 0,
                        "new_count": 0,
                        "skipped_count": 0,
                        "shortlisted_count": 0,
                        "duplicate_count": 0,
                        "errors": [],
                        "abort_reason": None,
                        "new_jobs": [],
                    }
                    for i, s_name in enumerate(clean_sites)
                ],
                "site_history": site_history,
            }
            write_scan_state(self.db_path, initial_state)

            cmd = [
                sys.executable,
                "-m",
                "job_hunter",
                f"--db={self.db_path}",
                f"--config-dir={self.config_dir}",
                f"--root={self.root}",
                "scan",
                f"--sites={','.join(clean_sites)}",
                f"--scan-id={scan_id}",
            ]
            if clean_query:
                cmd.append(f"--query={clean_query}")
            if dry_run:
                cmd.append("--dry-run")

            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(PROJECT_ROOT),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                )
            except BaseException:
                release_lock_and_stop_files(self.db_path, expected_scan_id=scan_id)
                initial_state["state"] = "failed"
                initial_state["error"] = "Failed to spawn scan subprocess"
                write_scan_state(self.db_path, initial_state)
                raise

            self._proc = proc
            initial_state["pid"] = proc.pid
            initial_state["launcher_pid"] = proc.pid

            # Hand lock ownership from dashboard PID to `proc.pid` unless child already adopted it.
            cur_lock = read_lock_info(self.db_path)
            if cur_lock and cur_lock.get("scan_id") == scan_id and cur_lock.get("pid") == os.getpid():
                _replace_lock_file(
                    scan_lock_path(self.db_path),
                    {
                        "pid": proc.pid,
                        "launcher_pid": proc.pid,
                        "scan_id": scan_id,
                        "started_at": _dt_to_iso(now_dt),
                    },
                )
                cur_st = read_scan_state(self.db_path)
                if cur_st.get("scan_id") == scan_id and cur_st.get("pid") is None:
                    write_scan_state(self.db_path, initial_state)

            return read_scan_state(self.db_path)

    def stop_scan(self, *, grace_seconds: float = 2.0) -> dict[str, Any]:
        """Signal cooperative stop, wait briefly, terminate subprocess if needed, and mark run stopped."""
        with self._mutex:
            self._reap_unlocked()
            state = read_scan_state(self.db_path)
            lock = read_lock_info(self.db_path)
            pid = state.get("pid") or (lock.get("pid") if lock else None)
            proc = self._proc

            is_active = (proc is not None and proc.poll() is None) or (
                isinstance(pid, int) and is_pid_alive(pid)
            )
            if not is_active and state.get("state") != "running":
                release_lock_and_stop_files(self.db_path)
                return state

            stop_path = scan_stop_path(self.db_path)
            stop_path.parent.mkdir(parents=True, exist_ok=True)
            stop_path.write_text("stop\n", encoding="utf-8")

        deadline = time.monotonic() + max(0.1, grace_seconds)
        while time.monotonic() < deadline:
            cur_st = read_scan_state(self.db_path)
            child_pid = cur_st.get("pid")
            proc_done = proc is None or proc.poll() is not None
            child_done = not (isinstance(child_pid, int) and is_pid_alive(child_pid))
            if proc_done and child_done:
                break
            time.sleep(0.05)

        with self._mutex:
            cur_st = read_scan_state(self.db_path)
            cur_lock = read_lock_info(self.db_path)
            child_pid = cur_st.get("pid") or (cur_lock.get("pid") if cur_lock else pid)
            if isinstance(child_pid, int) and is_pid_alive(child_pid):
                terminate_pid(child_pid)
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2.0)
            if isinstance(child_pid, int) and is_pid_alive(child_pid):
                for _ in range(20):
                    if not is_pid_alive(child_pid):
                        break
                    time.sleep(0.05)

            self._proc = None
            now_dt = utcnow()
            latest = read_scan_state(self.db_path)
            was_dry_run = bool(latest.get("dry_run", False))

            if latest.get("state") != "stopped":
                latest["state"] = "stopped"
                latest["finished_at"] = _dt_to_iso(now_dt)
                latest["current_site"] = None
                latest["current_query"] = None
                for sr in latest.get("site_results", []):
                    if isinstance(sr, dict) and sr.get("status") in ("running", "pending"):
                        sr["status"] = "stopped"
                        sr["abort_reason"] = "Stopped by user"
                        errs = list(sr.get("errors") or [])
                        if "Stopped by user" not in errs:
                            errs.append("Stopped by user")
                        sr["errors"] = errs
                write_scan_state(self.db_path, latest)

            if not was_dry_run and self.db_path.exists():
                with session_scope(self.session_factory) as s:
                    open_runs = list(s.scalars(select(Run).where(Run.finished_at.is_(None))).all())
                    for r in open_runs:
                        r.finished_at = now_dt
                        errs = list(r.errors or [])
                        if "Stopped by user" not in errs:
                            errs.append("Stopped by user")
                        r.errors = errs
                    # Record scan_stopped event if child was hard-terminated before writing it
                    recent_stopped = s.scalars(
                        select(Event)
                        .where(Event.action == "scan_stopped")
                        .order_by(Event.id.desc())
                        .limit(1)
                    ).first()
                    sid = latest.get("scan_id") or ""
                    if recent_stopped is None or (sid and f"scan_id={sid}" not in (recent_stopped.result or "")):
                        log_event(s, "scan_stopped", f"scan_id={sid} stopped by user")

            release_lock_and_stop_files(self.db_path)
            return latest
