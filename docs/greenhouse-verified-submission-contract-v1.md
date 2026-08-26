# Greenhouse Verified Submission Contract V1

The provider-specific contract required for Greenhouse to eventually earn
`submission_supported=True` -- built, tested against local fixtures, and left
**disabled by default**.

The headline outcome, stated plainly up front:

> **`GreenhouseApplicationProvider.capabilities.submission_supported` is still `False`.**
> This feature builds and proves the complete contract and a real, working submit
> engine -- but a local fixture is never sufficient evidence for real-provider
> production submission support. `submission_supported` stays `False` until a
> genuine, explicitly-authorized real-employer canary run has actually confirmed one.

---

## 1. Why this doesn't touch the existing ASSIST pipeline

Every existing safety property this project already relies on stays exactly as it was:

- `app.applications.browser_runtime` and `app.applications.browser_assist` are
  **completely unmodified**. Neither grows a submit-click capability; the standing
  rule that browser_runtime never clicks a final submit control for any real provider
  is untouched.
- `app.applications.executor.process_execution()`'s ordinary AUTO_PERMITTED/
  approved-submit pipeline never calls into any of this feature's new modules.
  Nothing here is reachable from that pipeline, from a worker, or from a scheduled
  loop.
- The only two ways this feature's submit engine can ever run are (a) this
  feature's own test suite, against local `file://` fixtures, and (b) the
  disabled-by-default canary, requiring an explicit per-job operator action.

This mirrors Phase 13's `app.applications.canary` precedent: a second module that
opens its **own**, independent Playwright session for a narrowly-scoped purpose,
reusing `browser_runtime`'s pure DOM-scan helpers without touching
`browser_runtime`'s own public API.

## 2. The four new modules

| Module | Role |
|---|---|
| `app.applications.greenhouse_submit_claim` | The submit-once claim ledger (migration 58, `greenhouse_submit_claims`). One atomic `UPDATE ... WHERE submit_attempted = 0` flip per execution -- the actual, physical "at most one submit action" guarantee. |
| `app.applications.greenhouse_submit_contract` | Pure, read-only pre-submit contract. Proves identity, liveness, form fingerprint, approval validity, document binding, and required-field completeness (steps 1-6); reports steps 7-8 (submit control uniqueness, claim) as `NOT_YET_CHECKED` unless given genuine `BrowserEvidence`. |
| `app.applications.greenhouse_submit_engine` | The actual submit engine. Reuses `browser_assist.start_session()`/`resume_session()` unchanged to reach `READY_FOR_FINAL_SUBMIT`, then performs the one physical click and classifies the outcome. |
| `app.applications.greenhouse_canary` | The gate. Requires `GREENHOUSE_SUBMIT_CANARY_ENABLED=true`, an explicit `confirm=True` on the specific call, a recognized Greenhouse identity, and a current ACTIVE durable approval -- before it will even call the engine. |

## 3. The submit contract (12 points)

1. **Canonical application/job identity** -- `providers_greenhouse.canonical_identity()`.
2. **Current posting still active** -- `check_job_still_active()` (genuine evidence or "not checkable", never a guess).
3. **Current form fingerprint** -- re-discovered fresh, compared against the execution's recorded fingerprint.
4. **Exact approved answer set** -- the durable `application_approvals` row, live-revalidated (`approval.is_current_valid`).
5. **Exact approved resume/cover-letter artifacts** -- `document_binding.verify_artifact_matches_job()` plus a hash comparison against the approval's recorded hash.
6. **Required fields complete** -- the normalized form's `unanswered_required()`/`high_risk_fields()`.
7. **Submit control uniquely identified** -- a DOM fact, only checkable once a browser is open; the engine refuses unless exactly one `FINAL_SUBMIT`-classified control is found.
8. **Submit-once execution claim acquired** -- the atomic claim-flip, the *last* thing acquired before the click.
9. **Single submit action performed** -- exactly one genuine Playwright `.click()`.
10. **Post-submit result classified** -- `CONFIRMED` / `REJECTED` / `BLOCKED` / `SUBMISSION_STATUS_UNKNOWN`.
11. **Confirmation evidence persisted** -- via the existing `confirmation_parser`/`confirmation_evidence` machinery, unmodified.
12. **Receipt created only for objectively confirmed submission** -- `receipts.record_receipt()`, only on `CONFIRMED`.

Steps 1-6 live in `greenhouse_submit_contract.build_submit_contract()`, callable
without ever opening a browser (used by the CLI's `greenhouse-contract` command and by
the engine's own pre-flight and immediately-before-click re-checks). Steps 7-12 are
the engine's own responsibility.

## 4. Outcome classification

| Observation | Outcome | Never retried automatically |
|---|---|---|
| Trusted success phrase (+corroborating id/URL) | `CONFIRMED` | -- (terminal, receipt recorded) |
| Server-side validation error markup on the resulting page | `REJECTED` | yes |
| CAPTCHA / login wall / stale approval / stale form / ambiguous submit control / duplicate-application phrase / already attempted | `BLOCKED` | yes -- no click was ever attempted (except "already attempted", where one was) |
| Click never dispatched (Playwright `TimeoutError` on an unresponsive control) | `SUBMISSION_STATUS_UNKNOWN` (`TIMEOUT_BEFORE_CLICK`) | yes |
| Click dispatched, no response observed before timeout | `SUBMISSION_STATUS_UNKNOWN` (`TIMEOUT_AFTER_CLICK`) | yes |
| Click dispatched, request failed at the network level | `SUBMISSION_STATUS_UNKNOWN` (`CONNECTION_LOST`) | yes |
| Click dispatched, response has no recognizable phrase | `SUBMISSION_STATUS_UNKNOWN` (`UNRECOGNIZED_OUTCOME`) | yes |

Every `SUBMISSION_STATUS_UNKNOWN` execution raises the existing
`BlockerCode.SUBMISSION_STATUS_UNKNOWN` blocker and stays `active=1` -- resolvable
only through the existing `app.applications.reconcile` human/operator path, exactly
like every other unknown-outcome path in this project.

## 5. The controlled canary

`app.applications.greenhouse_canary.run_greenhouse_submit_canary(job_id, confirm=True)`
is the **only** sanctioned way to invoke the engine against a real posting. Every gate
is checked before a browser is ever opened:

1. `config.GREENHOUSE_SUBMIT_CANARY_ENABLED` (default `False`).
2. `confirm=True` explicitly passed for this specific call.
3. Recognized Greenhouse identity + a current, ACTIVE, non-stale durable approval.
4. Playwright installed and `BROWSER_ASSIST_ENABLED` true.

The browser always runs visible (`headless=False`), regardless of the operator's
`BROWSER_HEADLESS` setting. There is no batch entry point, no scheduled entry point,
and no test in this project may set `GREENHOUSE_SUBMIT_CANARY_ENABLED = True` --
`tests/test_greenhouse_canary.py` proves only refusals; `tests/test_greenhouse_submit_engine.py`
exercises the engine directly against local fixtures.

## 6. Testing

- `tests/test_greenhouse_submit_claim.py` -- the atomic claim ledger (no browser).
- `tests/test_greenhouse_submit_contract.py` -- the 6 pure contract checks plus
  `BrowserEvidence` folding, against a mocked Greenhouse Job Board API (no live
  network), reusing the exact `provider_registry`/`httpx.MockTransport` pattern
  `tests/test_approval.py` already established for this provider.
- `tests/test_greenhouse_canary.py` -- every canary gate, proving only refusals.
- `tests/test_greenhouse_submit_engine.py` -- real Chromium-driven E2E tests (marked
  `browser`) against `tests/browser_fixtures.py::greenhouse_like_submit_flow_page()`,
  whose Submit button performs a `fetch()` to a fixed, fake
  `https://greenhouse-fixture.local/apply` endpoint that Playwright `page.route()`
  intercepts deterministically per scenario -- no real network call anywhere.
  Covers: successful submit, server validation error, duplicate-application
  handling, unrecognized response, timeout before/after the click, connection loss
  after the click, CAPTCHA, login wall, expired posting, double-submit protection,
  and an ambiguous (two-control) page.

No automated test enables the real canary or contacts a real employer.

## 7. Capability matrix (before / after)

| | Before | After |
|---|---|---|
| `discovery_supported` | True | True (unchanged) |
| `form_discovery_supported` | True (`PROVIDER_API`) | True (unchanged) |
| `fill_supported` | True (`PROVIDER_API`) | True (unchanged) |
| `upload_supported` | True (`PROVIDER_API`) | True (unchanged) |
| `assist_supported` | True (browser ASSIST) | True (unchanged) |
| `submission_supported` | **False** | **False** (unchanged -- see section 1) |
| `confirmation_supported` | via browser-assist observation | unchanged |

`app.applications.execution_contract` is untouched: `submission_supported` is still
read from `ApplicationCapabilities.submission_supported` alone, never inferred from
this feature's engine/canary existing.
