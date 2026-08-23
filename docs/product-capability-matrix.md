# Product capability matrix

CLAUDE.md production-v2 section 92: "Never let marketing/UI exceed this
matrix." This is the authoritative, evidence-based list of what this project
can actually do today, current as of this build
(`feat/production-autonomous-agent-v2`). "Fixture tested" means covered by
the automated test suite; "Live validated" means exercised against a real
external target (a real job board, a real provider API, a real ATS form) at
least once and the result recorded in this repo's docs/history.

| Capability | Implemented? | Fixture tested? | Live assist validated? | Live submit validated? | Human action required? | Known limitation |
|---|---|---|---|---|---|---|
| One-click START/STOP agent control | Yes | Yes | Yes (this session, isolated server + real browser clicks) | N/A | No | First cycle begins within ~1s of START (live-measured this session); recurring interval configurable, default 15m |
| Immediate first cycle on START | Yes | Yes | Yes (live-measured this session: `last_cycle_started_at` populated <1s after START) | N/A | No | none observed |
| Fresh job discovery (Greenhouse/Lever/Ashby/Workable/SmartRecruiters/Workday/Breezy) | Yes | Yes | Yes (see `docs/real-ats-validation.md`, `docs/phase3-ats-coverage.md`) | N/A | No | BambooHR/Recruitee/Comeet fixture-tested only, not live-verified against a real tenant as of this build |
| FULL_TIME hard gate | Yes | Yes | Yes | N/A | No (UNKNOWN routes to review, never silently treated as FULL_TIME) | Text-fallback classification requires explicit language; a JD with no employment-type signal anywhere stays UNKNOWN by design |
| Workday `timeType` structured employment extraction | Yes (added this session) | Yes | Yes (live-verified this session against `walmart.wd504.myworkdayjobs.com`) | N/A | No | Field is per-posting; a tenant/posting that omits it still falls back to text |
| Sponsorship classification (CONFIRMED/LIKELY/UNKNOWN/NO) | Yes | Yes | N/A (rule-based on JD text + historical dataset) | N/A | Yes for LIKELY (review) | Historical filings can only ever upgrade UNKNOWN->LIKELY, never CONFIRMED |
| JD requirement analysis (OR-groups, required/preferred/nice-to-have) | Yes | Yes | N/A | N/A | No | See `docs/phase14-resume-optimizer.md` for known edge cases |
| Truthful, evidence-linked resume generation | Yes | Yes | N/A | N/A | No | Claim checker blocks any unsupported bullet; a job whose evidence can't safely fit one page becomes `REVIEW_REQUIRED` |
| Exact one-page PDF resume | Yes | Yes | N/A | N/A | No (overflow -> `REVIEW_REQUIRED`, never a fabricated tiny render) | Bounded compression ladder; see `app/resume_optimizer/one_page.py` |
| ATS parseability check | Yes | Yes | N/A | N/A | No | Internal diagnostic only -- never a claimed universal ATS score |
| Application preparation (form discovery/mapping/fill draft) | Yes | Yes | Yes for Greenhouse/Lever/Ashby (see `docs/real-ats-validation.md`) | No | Yes when a field can't be answered from verified data | Lever exposes no structured question schema -- ASSIST_ONLY |
| Real ATS browser-assist (fill, pause at CAPTCHA/MFA/login/legal) | Yes | Yes | Yes (Phase 10-13 live Chromium runs against real Greenhouse/Lever/Ashby/SmartRecruiters/Workday postings) | No (by design) | Yes, whenever a blocker is hit | Closed shadow-root fields are UNSUPPORTED; some Workday tenants front bot protection this app will not bypass |
| Automatic legitimate submission (real employer) | Implemented as an architecture/gate, but **no real ATS adapter currently sets `submission_supported=True`** | Yes (via `MockATSProvider`) | N/A | **NONE** (see below) | Always, for any real provider today | This is the honest, current state -- not a placeholder oversight; see Phase 8 rule in CLAUDE.md |
| Mock ATS end-to-end submission (test/demo only) | Yes | Yes | N/A | Yes, `mock_ats` only | No | Never used for a real job; `is_test_fixture=1` keeps it out of the real dashboard by default |
| Duplicate-submission protection | Yes | Yes | N/A | N/A | No | Partial unique index (`active=1`), same pattern used everywhere in this project |
| Crash-safe submit-outcome handling (`SUBMISSION_STATUS_UNKNOWN`) | Yes | Yes | Yes (Phase 9 crash-recovery testing) | N/A | Yes (explicit reconcile only, never auto-retried) | none observed |
| Job identity verification before upload/submit | Yes | Yes | Yes (Phase 12-13 live runs) | N/A | Yes below `VERIFIED` confidence | `location` is a weak, corroborating-only signal |
| Application tracker / receipts | Yes | Yes | N/A | N/A | No | none observed |
| Needs Your Action queue (single authoritative source) | Yes (unified this session -- was previously two disagreeing sources, see final report) | Yes (new doctor regression check added this session) | Yes (live-verified this session) | N/A | Yes, by definition | none observed |
| Test-data isolation from real dashboard | Yes (added this session) | Yes | Yes (live-verified this session) | N/A | No | Applies to the `jobs` table and everything joined off it; does not retroactively hide rows a developer manually created with `provider != 'mock_ats'` before this session unless re-flagged |
| Live agent activity feed (lifecycle + cycle events) | Yes (added this session) | Yes | Yes (live-verified this session) | N/A | No | Best-effort logging (a log-write failure never blocks the cycle) |
| Provider capability matrix page | Yes (pre-existing) | Yes | N/A | N/A | No | none observed |
| SQLite + PostgreSQL parity | Yes | Yes | N/A | N/A | No | none observed |
| Application budgets (per-hour/day/company) | Yes | Yes | N/A | N/A | No | none observed |
| Advanced settings UI (sponsorship policy, interval, etc.) | Partial | Partial | N/A | N/A | N/A | `SPONSORSHIP_POLICY` (added this session) is `.env`-configured only -- no dashboard settings-editing page was built this session; see final report |
| Email/recruiter reply tracking | Architecture only (data model), no real inbox integration | N/A | N/A | N/A | N/A | Unchanged this session -- out of scope |

## Exact real auto-submit providers today

**NONE.** `app.applications.mock_ats.MockATSProvider` is the only
`ApplicationProvider` in this codebase with `submission_supported=True`. No
real ATS adapter (Greenhouse, Lever, Ashby, Workable, SmartRecruiters,
Workday, BambooHR, BreezyHR, Recruitee, Comeet) has been given that flag, and
this session did not change that. This is a deliberate, honest project state,
not an oversight -- see the Phase 8 durable rule in `CLAUDE.md`: a real
adapter may only ever set `submission_supported=True` after genuinely tested,
explicitly-permitted, end-to-end submission automation exists for it.
