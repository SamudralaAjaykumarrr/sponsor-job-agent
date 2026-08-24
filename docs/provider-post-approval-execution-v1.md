# Provider Post-Approval Execution V1 — Planning

Branch: `feat/provider-post-approval-execution-v1`. Base: `main` @ `34ebbee`
(approval-gated autonomy already merged).

## 1. What already exists (read in full before writing any code)

This codebase already implements almost the entire post-approval execution
contract the build brief asks for, across 14 prior phases plus the
approval-gated-autonomy-v1 feature:

- **Durable approval gate** (`app/applications/approval.py`,
  `application_approvals` table, migration 54): `approve_and_apply()` claims
  the execution atomically, records a fingerprint-bound approval row, then
  re-runs `executor.process_execution(approved=True)`, which independently
  re-verifies the approval is ACTIVE and every fingerprint (job identity, JD,
  resume variant/hash, answers_version, profile, form_fingerprint,
  sponsorship, employment type) still matches *immediately* before any
  `provider.submit()` call — `approved=True` alone is never trusted.
- **Two application layers, correctly separate**:
  - `app.applications.provider.ApplicationProvider` (network/API-based
    headless form discovery+fill+submit). Only `mock_ats` has
    `submission_supported=True`; Greenhouse is `PARTIAL`/`ASSIST_ONLY` (live
    form discovery via the public Job Board API, no public submit
    endpoint); Lever is `UNSUPPORTED` (no public question schema); Ashby/
    Workday/SmartRecruiters/Workable fall through to
    `GenericAssistOnlyProvider` (honest `UNSUPPORTED`).
  - `app.applications.browser_assist` / `browser_runtime` (real, visible
    Playwright browser). Provider-agnostic DOM engine, live-verified against
    real Greenhouse/Lever/Ashby/Workable postings (see
    `browser_capability_matrix.py`). **Structurally prevented from ever
    clicking a final submit/apply action for any real provider** — enforced
    by a static doctor check (`_check_no_browser_auto_submit_capability`)
    that scans the module's public API for forbidden name patterns. This is
    a durable, deliberate CLAUDE.md invariant (Phase 10-13), not a gap.
- **Job identity verification**, single-signal (`verify_job_identity`, wired
  into every navigation) and multi-signal (`verify_job_identity_full`,
  already wired at the two highest-stakes moments: immediately before a
  resume upload and immediately before `READY_FOR_FINAL_SUBMIT`).
- **Confirmation evidence grading** (STRONG/MODERATE/WEAK/NONE), checkpoints,
  provider assist health, capability evidence staleness, canaries, Workday
  per-tenant stability tracking — all already built and doctor-enforced.

**Conclusion that follows directly from the above, and from the durable,
explicitly-still-binding CLAUDE.md rule that `browser_runtime` may never
gain a final-submit capability for any real provider under any condition:**
none of Greenhouse/Lever/Ashby/Workday/SmartRecruiters/Workable can honestly
receive `final_submission_supported=True` in this build. Only the
deterministic `mock_ats` fixture may. Claiming otherwise for any real
provider would be exactly the "fake support" the build brief prohibits.
This plan does not attempt it, and does not weaken the doctor check that
would catch a regression here.

## 2. Genuine gaps this build closes

1. **No durable submission receipt model.** Confirmation evidence lives only
   as columns on `application_executions`/`browser_assist_sessions` — there
   is no dedicated, append-only receipt record capturing "what evidence
   proved this application was submitted", cross-linking the two possible
   confirmation paths (headless `mock_ats` auto-submit vs. browser-assist
   manual-submit-then-reconcile). Add migration 55: `application_receipts`.
2. **No bridge from APPROVE & APPLY to the browser-assist session.** Today,
   clicking APPROVE & APPLY for a real (ASSIST_ONLY) provider lands the
   execution on `APPROVED` and stops — the user must separately remember to
   click "Start Browser Assist" on the job detail page. Add
   `app/applications/post_approval.py`: a best-effort bridge, gated on
   `BROWSER_ASSIST_ENABLED`, that automatically opens (or resumes) the
   browser-assist session the moment an execution lands `APPROVED`, so
   approval actually *starts* the strongest safe automation immediately,
   matching the build brief's desired runtime, without weakening any
   existing gate (browser_assist re-derives eligibility/identity/CAPTCHA/
   login checks independently regardless of who calls it).
3. **Doctor coverage for the two additions above**, plus one explicit static
   guard that none of the six named real providers' `ApplicationCapabilities`
   / `ApplicationProvider.submit` ever get inflated to
   `submission_supported=True`.
4. **UI**: surface the auto-started browser session and any receipt inline
   on the job detail page's APPROVED banner; add a small Application
   Receipts page cross-linked from the existing three (deliberately
   separate) diagnostics pages (`/applications/capability-matrix`,
   `/applications/browser-capability-matrix`, `/applications/provider-health`).
5. **Tests**: receipts module, post-approval bridge (enabled/disabled,
   idempotent, concurrent-safe), new doctor checks, Postgres coverage for
   the new table, and a Playwright scenario exercising the auto-started
   session end to end against a local fixture.

## 3. Explicit non-goals (and why)

- No real ATS gets headless or browser-driven final-submit automation.
  Justified above; also what CLAUDE.md's Phase 10-13 rules and this
  session's own build brief ("DO NOT FAKE SUPPORT") require.
- No unattended background auto-approval or auto-starting of browser
  sessions across *multiple* jobs without a per-job human APPROVE & APPLY
  click. The build brief's desired runtime is triggered by "user clicks
  APPROVE & APPLY" (one job, one explicit action) — building unattended
  multi-job background browser automation would cross into exactly the
  "blind auto-apply" default this project has always rejected
  (`Default mode should be ASSIST, not blind auto-apply`). "Continue
  processing other jobs" is already true today: each job's execution/
  approval/browser session is independent, so approving job A never blocks
  preparing or approving job B.
- No fabricated per-provider confirmation phrase/URL tables. The existing
  confirmation engine is provider-agnostic by construction and already
  live-verified against real Greenhouse/Lever/Ashby/Workable pages; adding
  invented "Greenhouse-specific" success phrases without genuine observed
  evidence would violate the standing "never inflate without a genuine
  observation" rule. Provider-specific hardening in this build is receipts
  + doctor coverage, not fabricated heuristics.

## 4. Migration

`55_application_receipts_table` — additive, SQLite/Postgres-compatible via
the existing `id_column` pattern, idempotent (`CREATE TABLE IF NOT EXISTS`).

## 5. Work order

1. Migration 55 + `app/applications/receipts.py`.
2. Wire receipts into `executor.process_execution` (headless APPLIED path)
   and `browser_assist.attempt_user_submit_reconciliation` (manual-submit
   APPLIED path).
3. `app/applications/post_approval.py` bridge; wire into
   `approval.approve_and_apply`.
4. Doctor checks (`app/applications/doctor.py`).
5. UI: `job_detail.html` APPROVED banner, `main.py` + template for
   `/applications/receipts`, cross-links on the three diagnostics pages.
6. Tests: unit (receipts, bridge, doctor), Postgres, Playwright.
7. Full validation pass (see build brief section 21) + final report.
