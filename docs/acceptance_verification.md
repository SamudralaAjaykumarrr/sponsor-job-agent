# Acceptance Verification

Verified 2026-08-21. Every item below was actually executed, not assumed.

| Criterion | Evidence |
|---|---|
| App starts | `./start.sh` launched uvicorn; `curl /health` -> `{"status":"ok"}` |
| Dashboard loads | `curl /` -> HTTP 200, rendered HTML with job table |
| Manual JD ingestion works | `POST /jobs/ingest` (curl, live server) -> 303 redirect to new job detail page |
| Sponsorship classification works | `tests/test_sponsorship.py` (5 tests) + live curl: "Visa sponsorship available" -> CONFIRMED_SPONSOR; known employer -> LIKELY_SPONSOR; no match -> UNKNOWN |
| No-sponsorship jobs are skipped | `tests/test_pipeline.py::test_no_sponsorship_job_is_hard_skipped` + live curl: "unable to sponsor" -> NO_SPONSORSHIP, application_state=SKIPPED, no resume files generated |
| Work arrangement classification works | `tests/test_workarrangement.py` (5 tests): remote/hybrid/onsite/unknown, incl. "no remote" vs "remote" disambiguation |
| Freshness tracking works | `tests/test_freshness.py` (7 tests) covering all 5 tiers + fallback to first_seen_at + unparseable dates |
| Scoring works | `tests/test_matching_and_scoring.py` incl. remote+confirmed > onsite+likely, NO_SPONSORSHIP/UNKNOWN forced to score 0 |
| High-priority jobs are identified | Live job scored P1_REMOTE_CONFIRMED (120.0); dashboard "High Priority" filter maps to P1-P3 tiers |
| Tailored resume generation works | `tests/test_resume.py::test_generate_resume_only_uses_verified_data`; live server generated resume.docx/pdf/txt for a CONFIRMED_SPONSOR job |
| DOCX works | `write_docx` produces non-empty file (test + live curl download) |
| PDF works | `write_pdf` produces non-empty file (test + live curl download) |
| Unsupported claims are blocked | `tests/test_resume.py::test_claim_checker_blocks_fabricated_bullet/_skill/_employer` -- injected fabricated content is caught before it could reach output |
| Application answers work | `generate_application_answers` verified via live curl download of `application_answers.json` |
| Unknown factual answers become NEEDS_USER_INPUT | Live curl: blank profile -> every personal field in resume.txt and application_answers.json is literally `NEEDS_USER_INPUT`, never fabricated |
| Tracking works | `jobs` SQLite table with `application_state`, state-transition guard in `applications/tracker.py`, output files tracked 1:1 via `output/<job_id>/` + DB path columns |
| Tests pass | `pytest tests/ -q` -> 45 passed |
| `./start.sh` works | Ran directly; server bound to 127.0.0.1:8000 and responded to requests |
| No secrets committed | `candidate_data/` (private facts) and `data/app.db` are gitignored; verified with `git check-ignore -v`; no API keys/secrets in tracked files |

## Known MVP limitations (by design, per spec)

- Job ingestion is manual JD paste only -- no scraping/board integration (spec explicitly restricts automation to avoid ToS/anti-bot issues; AUTO mode is a stub for future use).
- `LIKELY_SPONSOR` reference list (`data/known_h1b_sponsors.json`) is a small illustrative bundled list, not a live USCIS data feed -- flagged as review-only everywhere it's used, never treated as proof.
- Candidate profile ships blank (`candidate_data/profile.json`, all `NEEDS_USER_INPUT`) since no real personal facts were provided -- this is the correct, truthful state per the "never fabricate" rule, not a bug.
