# Architecture

Modular monolith, Python 3.12 + FastAPI. No React/Kafka/Redis/Kubernetes/microservices.

## Modules (app/)

- `config.py` — paths, constants.
- `db.py` — SQLite connection + schema migration (raw sqlite3, no ORM needed for MVP).
- `models.py` — Pydantic models: Job, JobAnalysis, ApplicationAnswers, CandidateProfile.
- `candidate/profile.py` — loads private candidate JSON files from `candidate_data/`, resolves missing facts to `NEEDS_USER_INPUT`.
- `sponsorship/classifier.py` — rule-based classifier: CONFIRMED_SPONSOR / LIKELY_SPONSOR / UNKNOWN / NO_SPONSORSHIP. NO_SPONSORSHIP keywords checked first (hard skip). Likely list is a bundled local reference (`data/known_h1b_sponsors.json`) of employer names — historical sponsorship is NOT proof, so this only ever produces LIKELY_SPONSOR (review-only), never CONFIRMED.
- `workarrangement/classifier.py` — REMOTE / HYBRID / ONSITE from location + JD text.
- `freshness/tracker.py` — computes freshness tier from `published_at` (if reliable) else `first_seen_at`.
- `matching/skills.py` — extracts JD requirement keywords, matches against verified candidate skills/projects/experience, produces match score + gap list.
- `scoring/scorer.py` — combines remote/sponsorship/match/freshness into priority tier per CLAUDE.md ordering. Enforces hard gates (NO_SPONSORSHIP -> SKIPPED, UNKNOWN -> do not apply).
- `resume/generator.py` — builds resume content strictly from verified profile facts, selecting/reordering evidence relevant to the JD. Never invents. Unverified skills become gaps, not claims.
- `resume/claim_checker.py` — validates every generated resume line against the verified profile; blocks unsupported claims.
- `resume/docx_writer.py`, `resume/pdf_writer.py` — render resume.docx / resume.pdf / resume.txt.
- `applications/answers.py` — generates screener answers from verified profile fields; unknown factual answers -> `NEEDS_USER_INPUT`.
- `applications/tracker.py` — persists application state transitions (NEW -> ANALYZED -> READY_TO_APPLY -> APPLIED -> INTERVIEW / REJECTED / SKIPPED).
- `pipeline.py` — orchestrates ANALYZE / ASSIST modes end to end for one ingested JD.
- `main.py` — FastAPI app: manual JD ingestion endpoint, dashboard page (Jinja), filters, job detail, file downloads, state updates.

## Data flow

1. Manual JD ingestion (paste title/company/location/JD text/url/published_at) -> stored as `jobs` row, `first_seen_at = now`.
2. Pipeline runs: work-arrangement classification, sponsorship classification (hard skip if NO_SPONSORSHIP), freshness tier, skills match, priority scoring.
3. If sponsorship in {CONFIRMED_SPONSOR, LIKELY_SPONSOR} and mode is ASSIST: generate resume (docx/pdf/txt), job_analysis.json, application_answers.json, cover_letter.txt, mark READY_TO_APPLY (LIKELY_SPONSOR jobs are still flagged review-only in the UI even though files are prepared, per "review only" rule).
   - UNKNOWN sponsorship -> analyzed but NOT progressed to resume generation ("do not apply").
   - NO_SPONSORSHIP -> SKIPPED immediately, no further processing.
4. Dashboard lists/filters jobs; lets user change application_state (APPLIED/INTERVIEW/REJECTED) manually — no auto-submission anywhere.

## Storage layout

- `data/app.db` — SQLite.
- `data/known_h1b_sponsors.json` — bundled reference list (small, illustrative; not authoritative).
- `candidate_data/` — private candidate facts (gitignored). Missing fields = `"NEEDS_USER_INPUT"`.
- `output/<job_id>/` — resume.docx, resume.pdf, resume.txt, job_analysis.json, application_answers.json, cover_letter.txt (when useful).

## Explicitly out of scope for MVP

No LinkedIn/Indeed automation, no CAPTCHA/MFA/rate-limit/anti-bot bypass, no auto-apply (AUTO mode is a stub only), no job-board scraping (ingestion is manual JD paste for MVP — keeps the system inside "assist" boundaries and avoids ToS/anti-bot issues).
