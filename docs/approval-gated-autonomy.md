# Approval-Gated Autonomous Applications

Branch: `feat/approval-gated-autonomy-v1`

## Mission

START AGENT now runs the entire pipeline automatically -- discovery,
eligibility, sponsorship classification, JD analysis, one-page tailored
resume generation, claim/ATS validation, application-form discovery/fill/
validation -- and stops at exactly **one** normal human gate:
`READY_FOR_APPROVAL`. Nothing is ever submitted anywhere until the user
explicitly clicks **APPROVE & APPLY** for that specific job.

This is additive on top of the existing, unmodified Phase 8/9
`AUTO_PERMITTED`/`AUTO_SUBMIT_ENABLED` unattended-submission mechanism
(CLAUDE.md's durable rule: `AUTO_PERMITTED` may only submit when
`sponsorship_status == CONFIRMED_SPONSOR`, still enforced exactly as
before) -- approval is a **second**, human-triggered path to submission,
never a replacement of the first.

## State model

No new storage-level enum replaces the existing two-layer model
(`app.models.ApplicationState` / `app.applications.models.ExecutionStatus`).
Two new values were added, in the same style as every prior phase:

- `ExecutionStatus.APPROVED` / `ApplicationState.APPROVED` -- reached only
  after an explicit `APPROVE & APPLY` when the provider has no verified
  final-submission capability. Non-terminal (`active=1`, like
  `NEEDS_USER_ACTION`) -- the next real step is browser-assist or manual
  completion.

The existing `ExecutionStatus.SUBMISSION_READY` **is** the product-facing
`READY_FOR_APPROVAL` stage -- it already meant exactly that (form filled,
validated, auto-submit not permitted) before this feature; this feature
just gives it a first-class human action and an honest UI treatment
instead of a generic "needs your action" label.

`app/applications/product_state.py` is the single authoritative
display/predicate layer requested by the spec: `ProductStage` (the full
`DISCOVERED` .. `TRACKING` + exceptional-state vocabulary) and the five
named predicates (`ready_for_approval`, `approved_for_submission`,
`needs_user_action`, `submitted`, `confirmed`), each a pure function over
an already-fetched `application_executions` row. Dashboard/API code is
meant to use these instead of re-deriving the same logic.

## Durable approval record

`app/applications/approval.py` + migration 54 (`application_approvals`,
append-only, mirrors the `sponsorship_decisions`/`capability_evidence`
append-only pattern). `approve_and_apply(job_id)`:

1. Requires the job's active execution to genuinely be `SUBMISSION_READY`.
2. Atomically claims the execution FIRST (`SUBMISSION_READY -> STARTED`, an
   `UPDATE ... WHERE status = 'SUBMISSION_READY'` whose rowcount is the
   actual double-click/concurrent-tab guard -- a losing caller is told the
   application is already being processed, never re-runs the pipeline and
   never records anything).
3. Only the caller that wins that claim then records a durable row with
   fingerprints of everything being approved: job identity, JD, resume
   variant/hash, answers version, candidate profile hash, form fingerprint,
   sponsorship status, employment classification, and whether the provider
   had a verified submission capability at that moment. Claiming before
   recording (not the reverse) is deliberate: recording first would let two
   simultaneous clicks each successfully insert their own `ACTIVE`
   `application_approvals` row before either learned it lost the race,
   leaving two `ACTIVE` rows for one execution. With the claim first, only
   the winner ever records a row -- at most one `ACTIVE` approval per
   execution, with no separate lock and no destructive rewrite of any prior
   row (verified under real concurrent threads, see Tests below).
4. Re-runs the real, unmodified `app.applications.executor.process_execution`
   pipeline with `approved=True` -- every gate (eligibility, resume-hash,
   form/answers, job-still-active, rate limits, duplicates) is revalidated
   fresh, exactly as the existing `AUTO_PERMITTED` path already does.

`_approved_submit_permitted()` (`app/applications/executor.py`) is the one
new gate function this adds, parallel to the existing
`_auto_submit_permitted()`. It deliberately allows `LIKELY_SPONSOR` (the
human has already been shown "sponsorship history found -- verify before
applying" on the review card) but still requires a genuinely tested
`provider.capabilities.submission_supported` and a clean validation -- it
never fakes or forces a capability that doesn't exist, and it never
touches `config.APPLICATION_EXECUTOR_ENABLED` (continuing an
already-queued execution has never been gated by that flag anywhere in
this codebase; only creating a *new* one is).

**The `approved=True` boolean parameter is never sufficient by itself.**
The FIRST check inside `_approved_submit_permitted()` is
`app.applications.approval.verify_durable_approval_for_submission(job,
execution)` -- a server-side gate that runs immediately before
`process_execution()` would call `provider.submit()`, and that:

- re-fetches the LATEST `application_approvals` row fresh from the database
  for this execution (never trusts a value cached on the `execution` dict,
  and never trusts that the caller only reaches this code path when a real
  approval exists);
- requires that row to exist at all (a missing row always blocks, even if
  `approved=True` was passed straight into `process_execution()`, e.g. by a
  future caller other than `approve_and_apply()`);
- requires its `status` column to be `'ACTIVE'`;
- and requires `is_current_valid(job, execution, approval)` to report the
  approval still current -- job identity, JD fingerprint, resume variant/
  fingerprint, answers_version, candidate-profile fingerprint,
  form_fingerprint, sponsorship status, and employment classification must
  ALL still match what was approved.

`process_execution()` passes a FRESH read of the execution row
(`repo.get_execution(execution_id)`) into this check, not the `execution`
dict captured at the top of the call -- `form_fingerprint`/`answers_version`
are written by the FORM_DISCOVERED/snapshot-answers steps earlier in that
same call, so the pre-pipeline snapshot would be stale by the time the
submit gate runs.

### Approval invalidation

Validity is **never** a stored boolean -- `is_current_valid(job, execution,
approval)` recomputes live from the latest approval row's stored
fingerprints against the job/execution's CURRENT values: job identity, JD,
resume hash/variant, candidate-profile hash, **answers_version**, **form
fingerprint**, sponsorship status, and employment classification. This is
the same "never cached, always live-recomputed" idiom
`provider_health.compute_health()` already uses.

`answers_version` and `form_fingerprint` are compared with plain equality
(including empty-vs-non-empty), unlike the JD/resume checks above (which
only compare when both sides are already known) -- by the time an
execution reaches `SUBMISSION_READY` both are always genuinely populated,
so an approved value that is empty means "unknown/unset at approval time",
and the comparison is deliberately conservative in both directions: a
known value that changed to something else invalidates, and an
unknown-at-approval value that has since become known (or simply
non-empty) also invalidates -- it is never silently treated as still
valid. Only unknown-at-approval staying unknown-now is a non-event.

Two independent mechanisms make it impossible to submit on stale
authorization even without a background sweep:

- `verify_durable_approval_for_submission()` (above) is called
  synchronously, in-process, immediately before every `provider.submit()`
  call reachable via the approved path -- not merely "the pipeline happens
  to re-verify some things"; it is the one dedicated gate that must pass.
- `check_approval_freshness(job_id)` is surfaced on the job detail page
  for a resting `APPROVED` execution (a provider without submission
  support), showing a "content changed since approval -- re-review" banner
  without silently reverting any state.

## Dashboard / UI

- **READY FOR APPROVAL** section on the primary dashboard (`dashboard.html`,
  `#ready-for-approval`): cards with company/title/location/employment
  type/sponsorship (with the historical-sponsorship warning)/match score,
  the checklist (JD analyzed, resume tailored, 1 page, claim check, ATS
  parse, application filled, required fields validated), and
  Preview Resume / Review Application / **APPROVE & APPLY** buttons.
- **APPROVE & APPLY SELECTED**: checkbox selection + a confirmation modal
  listing every selected job before submitting; each job is still
  individually approved (`POST /applications/approve-bulk` ->
  `approve_and_apply_bulk`, one job's failure never stops the others).
- The job detail page (`job_detail.html`) is the Application Review Page --
  it already had JOB/SPONSORSHIP/MATCH/RESUME sections; this feature adds
  the `READY FOR APPROVAL` banner + primary `APPROVE & APPLY` CTA, and an
  `APPROVED` banner (with a staleness warning when applicable) for
  providers without submission support.
- Needs Action queue bug fix: `app.pipeline_dashboard._NEEDS_ACTION_QUERIES`
  used to key off `application_executions.requires_user_action = 1`, which
  a plain `SUBMISSION_READY` item also sets -- it no longer surfaces there;
  Needs Action is scoped to `NEEDS_USER_ACTION` / `VALIDATION_REQUIRED` /
  `SUBMISSION_STATUS_UNKNOWN` only, matching
  `app.applications.repo.DASHBOARD_BUCKETS["needs_action"]`.
- Test-mode isolation: `build_ready_for_approval_queue()` excludes
  `is_test_fixture=1` rows by default, same `?include_test_data=true`
  opt-in toggle the dashboard's existing "view test job" link already used
  -- the TEST MODE fixture can still be approved end-to-end through the
  real UI without ever appearing in the default real-mode view.
- Tracker gained an `APPROVED` lane.

## Orchestrator change

`app/agent/orchestrator.py` no longer raises `AUTO_SUBMIT_ENABLED` in TEST
MODE (or ever). Every stage still runs fully automatically through
form-fill/validation and stops at `SUBMISSION_READY`; reaching `APPLIED`
from there -- in TEST MODE or real mode -- always requires the same real
`approve_and_apply()` call a human click makes. `AUTO_SUBMIT_ENABLED`
itself is untouched by this feature and stays available for the
independent, pre-existing `AUTO_PERMITTED` mechanism.

## Doctor checks added (`app/applications/doctor.py`)

- `approved_status_without_approval_record` -- `APPROVED` never reachable
  without a genuine `application_approvals` row.
- `approval_submitted_for_unsupported_provider` -- no execution with a
  recorded approval may have reached `SUBMITTING`/`SUBMITTED`/`APPLIED`
  when its approval's `submission_capability` was `UNSUPPORTED`.
- `ready_for_approval_flagged_as_needs_action` -- regression guard for the
  Needs Action query fix above.

## Tests

- `tests/test_approval.py` -- the approval mechanism in isolation:
  ready-for-approval reached without ever hitting Needs Action, approve
  reaches `APPLIED` for `mock_ats`, idempotent double-click, atomic
  concurrent-claim guard, bulk approval isolates one failure, invalidation
  detection (resume/answers/JD/sponsorship/**answers_version**/**form_
  fingerprint**), the real-world Greenhouse case (mocked `httpx` transport,
  no live network) landing on `APPROVED` rather than faking a submission,
  the durable submit-time gate rejecting a missing approval row, a
  non-`ACTIVE` approval row, and a stale approval (proving `provider.
  submit()` is never called in any of those cases even when `approved=True`
  is passed directly into `process_execution()`), and real-thread
  concurrency proof that N simultaneous `approve_and_apply()` calls for one
  job produce at most one `provider.submit()` call and exactly one `ACTIVE`
  `application_approvals` row.
- `tests/test_agent_orchestrator.py` -- the old
  `test_full_test_mode_cycle_reaches_applied` (which asserted TEST MODE
  went straight to `APPLIED`) is now split into
  `test_full_test_mode_cycle_reaches_ready_for_approval` (asserts no
  submission happened) and `test_full_test_mode_cycle_then_approve_reaches_applied`
  (asserts the explicit approval is what completes it).
- `tests/test_approval_playwright.py` -- real Chromium, real app: Dashboard
  -> START AGENT (TEST MODE) -> wait for the READY FOR APPROVAL card ->
  Review Application -> verify 1-page/claim-check indicators and "no
  submission yet" -> APPROVE & APPLY -> APPLIED + confirmation shown.

## Known limitations

- Real ATS providers (Greenhouse, Lever, etc.) still have
  `submission_supported=False` -- unchanged, since no new live-verified
  final-submission capability was implemented for any of them in this
  branch. Approving one of those lands on `APPROVED` with an honest
  "requires browser assist / manual completion" message, never a fake
  `APPLIED`.
- Bulk approval's confirmation modal is client-side only (vanilla JS in
  `dashboard.html`); each individual approval still goes through the same
  server-side `approve_and_apply()` safety checks.
- Approval invalidation is live-recomputed at the points that matter
  (pipeline re-verification on approval, freshness check on the review
  page for a resting `APPROVED` execution) rather than an active
  background sweep that mutates `APPROVED` back to `SUBMISSION_READY` the
  instant something changes elsewhere.
