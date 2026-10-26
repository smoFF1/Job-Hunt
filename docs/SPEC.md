# Job Hunter — Specification

## Purpose
Local tool that (1) scans Israeli job boards daily, (2) filters and ranks jobs for an
entry-level software engineer, (3) logs everything to SQLite shown in a local dashboard,
and (4) ON DEMAND pre-fills application forms in a VISIBLE browser and STOPS before
submission. The human always submits.

## Tech stack
Python 3.11+, Playwright (scraping; later the visible pre-fill browser), SQLite via
SQLAlchemy, PyYAML, pdfplumber/pypdf, pytest, ruff. Dashboard (Phase 3): FastAPI + Jinja2 (+htmx), localhost only.

## Candidate data & CVs
- config/profile.yaml: personal details (name, email, phones, location, links, education, level). Single source of truth.
- CVs are INPUT to the system, not hardcoded. Files live in assets/cv/*.pdf and are registered in config/cvs.yaml: id, file, label, tags (e.g. [fullstack, ai, embedded]), default.
- cv_loader: extracts text from each PDF, derives a skill list, caches by file SHA-256, re-extracts automatically when a file changes.
- Match scoring uses union(profile core_skills, skills extracted from the selected CV).
- Per job: cv_id = suggested by tag match with the job's category, user-overridable in the dashboard; default CV otherwise. The applicator attaches the selected CV.
- Adding a new CV version = drop a file in assets/cv/ + one entry in cvs.yaml. No code changes.

## Architecture (extensibility is a core requirement)
job_hunter/
  config/        profile.yaml, cvs.yaml, search.yaml (roles, keywords EN+HE, sites, thresholds)
  core/          models.py, db.py, dedup.py, filters.py, scoring.py, cv_loader.py
  scrapers/      base.py (BaseScraper: search(query)->list[RawJob], fetch_detail(url)->JobDetail), registry.py, <site>.py
  applicants/    base.py (BaseApplicator: detect_form, fill, stop_before_submit), generic.py, <ats>.py
  dashboard/     FastAPI app + templates
  scripts/       login.py <site>, run_daily.py, seed.py
  tests/         with saved HTML fixtures per site
ADDING A NEW SITE = one new file in scrapers/ + one entry in search.yaml. No changes to core/.

## Data model
jobs: id, fingerprint (hash of normalized company+title+location), source, source_job_id, url, apply_url,
  company, title, location, description_text, language, posted_at, first_seen_at, last_seen_at,
  years_required_min, matched_skills (json), match_score (0-100), cv_id, status, status_reason, notes, updated_at
cv_versions: id, file, sha256, extracted_text, skills_json, added_at, is_default
events: id, timestamp, job_id, action, result
runs: id, started_at, finished_at, site, new_count, skipped_count, shortlisted_count, errors
Statuses: New, Skipped, Shortlisted, Needs Input, Ready for Review, Applied, Rejected, Interview, Failed, Expired

## Filtering spec (deterministic first; LLM only as optional tiebreaker behind a toggle)
- SKIP by TITLE (word-boundary match, case-insensitive): senior, sr, lead, principal, staff, architect, manager, director, head of, VP; HE: בכיר, ראש צוות, ראש קבוצה, ארכיטקט, מנהל, מנהלת. Reason format: "Skipped - Senior Role".
- SKIP by EXPERIENCE (from description): extract minimum required years (EN + HE patterns: "3+ years", "2-4 years", "at least 3 years", "לפחות 3 שנות ניסיון"...). Skip if years_required_min >= EXPERIENCE_CUTOFF (config, default 2). "0-2" and "1-2" pass. No years stated = pass.
- SKIP student-only positions (student / סטודנט) unless "graduate / בוגר" also appears.
- BOOST: junior, graduate, entry level, בוגר/בוגרי, ג'וניור, 0-2 years, ללא ניסיון.
- SCORE 0-100 = title/role match (30) + skill overlap (30) + seniority signals (20) + domain bonus for defense/security/embedded (10) + recency (10). Shortlist if score >= SHORTLIST_THRESHOLD (config, default 50).
- Target roles: QA Automation, AI Developer/Engineer, Fullstack, Backend, Frontend, DevOps, RT/Embedded, software roles in defense/security. Location: all of Israel.
- Every decision stores a human-readable status_reason.

## Phases
1. Foundation (no scraping): skeleton, config, DB schema, cv_loader, filter/scoring engine + tests, CLI, seed script.
2. Scrapers: one site at a time, starting with AllJobs or Drushim. Fixtures for tests, login.py for manual login.
3. Dashboard: table, filters, status/CV dropdowns, notes, CSV export, Hebrew/RTL.
4. Applicator: on-demand pre-fill in a visible browser, stop before submit.
5. Daily run: run_daily.py with lock file, catch-up, summary; scheduling via OS scheduler or /schedule running the script.