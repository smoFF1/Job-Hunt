# Job Hunter

Personal tool: scans Israeli job boards, filters/ranks entry-level software jobs, logs to SQLite,
and (later) pre-fills applications in a visible browser. **The human always submits.**
See [docs/SPEC.md](docs/SPEC.md) and [AGENTS.md](AGENTS.md).

**Status:** Phase 1 (foundation) — config, DB, CV loader, filter/scoring engine, CLI, seed data. No scraping yet.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
copy config\profile.example.yaml config\profile.yaml   # then fill in real values (gitignored)
copy config\cvs.example.yaml config\cvs.yaml           # point `file` at your PDF (gitignored)
copy .env.example .env                                   # optional: custom DB path
```

Put CV PDFs in `assets/cv/` (gitignored) and register them in `config/cvs.yaml`.

## CLI

```powershell
python -m job_hunter init-db                 # create schema (idempotent)
python -m job_hunter load-cvs                # sync cvs.yaml -> DB, show extracted skills
python -m job_hunter seed                    # 30 fake jobs (idempotent)
python -m job_hunter process                 # filter + score unprocessed jobs
python -m job_hunter rescore                 # after config changes (New/Skipped/Shortlisted only)
python -m job_hunter list --status Shortlisted --min-score 60
python -m job_hunter explain 9               # full decision + score breakdown
python -m job_hunter stats
```

Global options: `--db PATH`, `--config-dir DIR`, `-v`.

## Config

| File | Purpose |
|---|---|
| `config/profile.yaml` | Candidate details (gitignored). Template: `profile.example.yaml` |
| `config/cvs.yaml` | CV registry: id, file, label, tags, default (gitignored). Template: `cvs.example.yaml` |
| `config/search.yaml` | Thresholds, skip/boost keywords (EN+HE), categories, scoring points, sites |
| `config/skills.yaml` | Skills vocabulary: canonical name + aliases |

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check .
```
