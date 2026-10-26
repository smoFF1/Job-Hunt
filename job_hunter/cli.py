"""Command-line interface: python -m job_hunter <command> [options]."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from job_hunter import PROJECT_ROOT
from job_hunter.config import AppConfig, ConfigError, load_config
from job_hunter.core.cv_loader import CVRecord, sync_cvs
from job_hunter.core.db import get_engine, init_db, log_event, make_session_factory, session_scope
from job_hunter.core.models import STATUSES, Job, Run
from job_hunter.core.pipeline import Evaluator, process_pending, rescore, run_postings
from job_hunter.core.skills import SkillMatcher

log = logging.getLogger("job_hunter")


class Context:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.cfg: AppConfig = load_config(args.config_dir, args.root)
        self.engine = get_engine(args.db)
        init_db(self.engine)
        self.factory = make_session_factory(self.engine)
        self.matcher = SkillMatcher(self.cfg.skills)
        for w in self.cfg.warnings:
            log.warning(w)

    def load_cvs(self, session: Session) -> list[CVRecord]:
        return sync_cvs(session, self.cfg, self.matcher)

    def evaluator(self, session: Session) -> Evaluator:
        return Evaluator(self.cfg, self.load_cvs(session), self.matcher)


def _out(text: str = "") -> None:
    # Hebrew-safe output on Windows consoles
    try:
        sys.stdout.write(text + "\n")
    except UnicodeEncodeError:
        sys.stdout.buffer.write((text + "\n").encode("utf-8", errors="replace"))


# ----------------------------------------------------------------------------- commands

def cmd_init_db(ctx: Context) -> int:
    with session_scope(ctx.factory) as s:
        log_event(s, "db_init", f"schema ensured at {ctx.engine.url.database}")
    _out(f"Database ready: {ctx.engine.url.database}")
    return 0


def cmd_load_cvs(ctx: Context) -> int:
    with session_scope(ctx.factory) as s:
        records = ctx.load_cvs(s)
    if not records:
        _out("No CVs registered in config/cvs.yaml.")
    for r in records:
        state = "OK" if r.ok else f"FALLBACK ({r.problem})"
        _out(f"[{r.id}] {r.label} {'(default)' if r.default else ''} tags={r.tags} -> {state}")
        _out(f"    skills ({len(r.skills)}): {', '.join(r.skills) or '-'}")
    return 0


def cmd_seed(ctx: Context) -> int:
    from job_hunter.seed_data import SEED_JOBS, seed_postings

    with session_scope(ctx.factory) as s:
        run = run_postings(s, ctx.evaluator(s), seed_postings(), site="seed")
        _out(f"Seed run {run.id}: {len(SEED_JOBS)} postings -> new={run.new_count} "
             f"skipped={run.skipped_count} shortlisted={run.shortlisted_count} errors={len(run.errors)}")
    return 0


def cmd_process(ctx: Context) -> int:
    with session_scope(ctx.factory) as s:
        counts = process_pending(s, ctx.evaluator(s))
    _out(f"Processed {counts['processed']}: shortlisted={counts['Shortlisted']} "
         f"new={counts['New']} skipped={counts['Skipped']}")
    return 0


def cmd_rescore(ctx: Context) -> int:
    with session_scope(ctx.factory) as s:
        counts = rescore(s, ctx.evaluator(s))
    _out(f"Rescored: checked={counts['checked']} changed={counts['changed']} "
         "(only New/Skipped/Shortlisted are touched)")
    return 0


def _trunc(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def cmd_list(ctx: Context) -> int:
    a = ctx.args
    with session_scope(ctx.factory) as s:
        q = select(Job)
        if a.status:
            q = q.where(Job.status == a.status)
        if a.min_score is not None:
            q = q.where(Job.match_score >= a.min_score)
        q = q.order_by(Job.match_score.desc().nulls_last(), Job.id).limit(a.limit)
        jobs = s.scalars(q).all()
    _out(f"{'ID':>4}  {'STATUS':<12} {'SCORE':>5}  {'CV':<6} {'TITLE':<40} {'COMPANY':<22} SOURCE")
    for j in jobs:
        score = "-" if j.match_score is None else str(j.match_score)
        _out(f"{j.id:>4}  {j.status:<12} {score:>5}  {(j.cv_id or '-'):<6} {_trunc(j.title, 40):<40} "
             f"{_trunc(j.company, 22):<22} {j.source}")
    _out(f"({len(jobs)} jobs)")
    return 0


def cmd_explain(ctx: Context) -> int:
    with session_scope(ctx.factory) as s:
        job = s.get(Job, ctx.args.job_id)
        if job is None:
            _out(f"No job with id {ctx.args.job_id}")
            return 1
        ev = ctx.evaluator(s).evaluate_job(job)
        b = ev.breakdown
        _out(f"Job {job.id}: {job.title} @ {job.company} ({job.location}) [{job.source}]")
        _out(f"  url: {job.url}")
        _out(f"  stored : {job.status} | score {job.match_score} | cv {job.cv_id}")
        _out(f"  reason : {job.status_reason}")
        _out(f"  current config would give: {ev.status} | score {ev.score} | cv {ev.cv_id}")
        _out(f"  category: {ev.category.category.name} (matched in {ev.category.where or '-'}: "
             f"{ev.category.matched or '-'})")
        _out(f"  years_required_min: {ev.decision.years_min} (evidence: {ev.decision.experience_evidence})")
        if ev.decision.skip:
            _out(f"  skip rule: {ev.decision.rule} -> {ev.decision.reason}")
        for part in ("role", "skills", "seniority", "domain", "recency", "penalty"):
            _out(f"  {part:<10} {getattr(b, part):>3}  {b.notes.get(part, '')}")
        _out(f"  {'TOTAL':<10} {b.total:>3}  (threshold {ctx.cfg.search.shortlist_threshold})")
        _out(f"  job skills: {', '.join(ev.job_skills) or '-'}")
        _out(f"  matched   : {', '.join(ev.matched_skills) or '-'}")
    return 0


def cmd_stats(ctx: Context) -> int:
    with session_scope(ctx.factory) as s:
        rows = dict(s.execute(select(Job.status, func.count()).group_by(Job.status)).all())
        total = sum(rows.values())
        _out(f"Jobs: {total}")
        for st in STATUSES:
            if rows.get(st):
                _out(f"  {st:<17} {rows[st]}")
        runs = s.scalars(select(Run).order_by(Run.id.desc()).limit(5)).all()
        if runs:
            _out("Recent runs:")
            for r in runs:
                _out(f"  #{r.id} {r.site:<8} {r.started_at:%Y-%m-%d %H:%M} new={r.new_count} "
                     f"skipped={r.skipped_count} shortlisted={r.shortlisted_count} "
                     f"errors={len(r.errors or [])}")
    return 0


COMMANDS = {
    "init-db": (cmd_init_db, "Create the database schema (idempotent)"),
    "load-cvs": (cmd_load_cvs, "Sync config/cvs.yaml into cv_versions and show extracted skills"),
    "seed": (cmd_seed, "Insert and process the 30 fake seed jobs (idempotent)"),
    "process": (cmd_process, "Filter and score jobs that were not processed yet"),
    "rescore": (cmd_rescore, "Re-run filter/score after a config change (New/Skipped/Shortlisted only)"),
    "list": (cmd_list, "Show jobs in a terminal table"),
    "explain": (cmd_explain, "Show the full decision and score breakdown for a job"),
    "stats": (cmd_stats, "Counts by status and recent runs"),
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="job_hunter", description="Job Hunter CLI (Phase 1: foundation)")
    p.add_argument("--db", type=Path, default=None, help="SQLite DB path (default: $JOB_HUNTER_DB or "
                   "data/job_hunter.db)")
    p.add_argument("--config-dir", type=Path, default=None, help="Config directory (default: ./config)")
    p.add_argument("--root", type=Path, default=PROJECT_ROOT, help="Project root for CV paths")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    for name, (_fn, help_text) in COMMANDS.items():
        sp = sub.add_parser(name, help=help_text)
        if name == "list":
            sp.add_argument("--status", choices=STATUSES)
            sp.add_argument("--min-score", type=int)
            sp.add_argument("--limit", type=int, default=50)
        if name == "explain":
            sp.add_argument("job_id", type=int)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s: %(message)s")
    try:
        ctx = Context(args)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    fn, _ = COMMANDS[args.command]
    return fn(ctx)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
