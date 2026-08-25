# Real Provider Execution V1

Production-quality provider **execution** architecture for real ATSes, starting with
Greenhouse and Lever, with final submission deliberately left **disabled**.

The headline outcome, stated plainly up front:

> **`submission_supported` is `False` for Greenhouse, Lever, Ashby, Workday,
> SmartRecruiters and Workable — and stays that way.** It is `True` only for the
> deterministic in-process `mock_ats` fixture. This feature makes the *fill/assist*
> path production-quality; it does not, and must not, make real submission automatic.

---

## 1. Why a separate execution contract

Seven distinct capability facts already existed in this repo, but scattered across three
registries no single caller read together:

| Registry | Owns |
|---|---|
| `app.providers.capabilities.ProviderCapabilities` | discovery (can we *find* this provider's postings) |
| `app.applications.models.ApplicationCapabilities` | the headless network-API application adapters |
| `app.applications.browser_capability_matrix` | what the real-browser ASSIST engine has genuinely been observed doing |

`app.applications.execution_contract` is a strictly **derived, read-only projection** over
those three. It owns no facts, so it cannot inflate one:

```
discovery_supported   form_discovery_supported   fill_supported   upload_supported
assist_supported      submission_supported       confirmation_supported
```

Every "either source counts" flag carries an explicit `*_source`
(`PROVIDER_API` / `BROWSER_LIVE_VERIFIED` / `BROWSER_FIXTURE_ONLY` / `MOCK_FIXTURE` /
`NONE`), so "true" never leaves a reader guessing whether it came from a published API or
a live DOM observation.

### The one rule that matters most

`submission_supported` reads **exactly one field** —
`ApplicationCapabilities.submission_supported` — via `_submission_supported()`. It is never
OR-ed with, upgraded by, or inferred from any browser/assist capability. Browser fill
capability is *not* submission capability. This is enforced structurally (one field read),
plus two doctor checks:

- `_check_execution_contract_consistency` re-derives every flag and fails on drift.
- `_check_execution_contract_submission_never_inferred` fails if any provider but
  `mock_ats` reports `submission_supported=True`, or if a `True` is sourced from anything
  other than its own `ApplicationCapabilities` row.

Surfaces: `python -m app.applications.cli capability-audit`,
`/applications/execution-contract`, `/api/applications/execution-contract`.

---

## 2. Greenhouse

Built strictly on the same documented public read API the discovery connector already uses
(`boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}?questions=true`). No new interface,
no scraping of the apply flow.

- **Canonical identity** — `canonical_identity()` yields `(board_token, posting_id)` plus a
  canonical board URL, from the job row or parsed from a genuine `greenhouse.io` URL. Both
  real host shapes parse (`boards.greenhouse.io` and the newer `job-boards.greenhouse.io`,
  a real organic migration this project observed live). Nothing is fabricated from an
  unrelated URL.
- **Typed discovery outcome** — `discover_form_detailed()` returns
  `DISCOVERED` / `NOT_APPLICABLE` / `JOB_GONE` / `ACCESS_REFUSED` / `TEMPORARY_FAILURE` /
  `NO_QUESTIONS_EXPOSED`. A permanently-gone posting (404/410) is now distinguishable from a
  transient blip, instead of both collapsing into `discover_form() -> None`.
  `discover_form()` keeps its exact prior signature and behaviour — the
  `ApplicationProvider` contract is unchanged.
- **Liveness** — `check_job_still_active()` returns `False` only on genuine permanent
  evidence (404/410), `True` on a successful lookup, and `None` ("not checkable") for a
  timeout / 5xx / 403. `classify_job_inactive_reason()` distinguishes only what the API
  actually says: `410 -> EXPIRED`, `404 -> REMOVED`, otherwise `None`.
- **Normalized form** — `normalized_form()` projects the published schema into the shared
  provider-neutral model.

**Deliberately not claimed:** CAPTCHA, auth-wall and confirmation-page detection are
impossible on a JSON read API. They are the browser layer's observations, and inventing an
API-side signal for them would be an inflated capability.

## 3. Lever

Lever's public postings API exposes only `hostedUrl`/`applyUrl` — **no structured question
schema anywhere**. API-side form discovery therefore stays honestly `False`; no hardcoded
"typical Lever form" template was introduced.

That is a limitation of the API, not of this project's ability to reach the form: the
generic real-browser engine reads the rendered DOM directly and has live-verified 22 real
fields on Lever's own public demo posting. The unified contract reports exactly that —
`form_discovery_supported=True` with `form_discovery_source=BROWSER_LIVE_VERIFIED`, while
the adapter's own `ApplicationCapabilities.form_discovery_supported` stays `False`. The two
describe different interfaces and must not be conflated.

Added on the same public read API: canonical `(site, UUID posting id)` identity (a posting
id is only accepted when it is a real UUID — Lever's actual shape), `apply_url()` resolution
preferring the published `applyUrl`, and the same evidence-only
`check_job_still_active()` / `classify_job_inactive_reason()` pair.

Provider *selection* stays broad (any Lever job still gets the adapter, so the candidate
gets the apply URL); only the API-backed lookups are gated on a confident canonical identity.

---

## 4. Normalized application form model

`app.applications.form_model` unifies the two previously divergent representations of "the
fields on a real form":

- `FormSnapshot`/`FormField` — a provider API's published schema (Greenhouse, mock ATS)
- raw DOM dicts from `browser_runtime._detect_fields()` — the only path that reaches Lever,
  Ashby, Workable, …

Both project into one `NormalizedFormField` carrying provider field id, label, normalized
semantic type, input type, required flag, choices, current value, value source/evidence,
confidence, `safe_answer_available`, and high-risk classification.

It is **not** a second matching engine (it delegates to the same `match_field` +
`find_field` pair every adapter uses) and **not** a filling policy —
`safe_answer_available` *reports* what the existing rules would allow and never widens them.

### High-risk classification

The brief's high-risk list is a closed `HighRiskClass` vocabulary computed here as an
explicit recorded property — **not** by widening `SENSITIVE_CATEGORIES`, which would have
silently changed what actually gets *filled*. A high-risk field with a genuinely
authoritative profile answer (salary, relocation) is reported high-risk *with*
`authoritatively_known=True`, matching the brief's own "where not already authoritatively
known" qualifier. Sensitive categories never count as authoritatively known. Certification
claims are never derivable — the profile models none.

---

## 5. Document upload binding (migration 57)

`application_document_bindings` is the durable, append-only proof of **which artifact went
to which provider field for which job**: job id, execution/session, provider, document kind,
path, filename, SHA-256, resume variant id, provider field id/label, checkpoint, verified
flag, timestamp.

- `verify_artifact_matches_job()` is the "never silently substitute another resume" guard —
  a pure function using the same `/<job_id>/` path-segment convention every other
  resume-ownership check in this project agrees on (so the optimizer's nested
  `output/<job_id>/optimized/<variant_id>/` layout is accepted).
- The **browser-assist** path records `verified=1` — the file genuinely was accepted by a
  live form field.
- The **executor draft** path records `verified=0` — it prepares a draft and performs no
  network upload, so claiming a verified upload would be inflated evidence.
- A failing ownership check is recorded as an *unverified* binding with the reason, never
  silently dropped: an audit log that omits the suspicious case is worse than useless.
- Two doctor checks: `document_binding_wrong_job` and
  `document_binding_execution_job_mismatch`.

---

## 6. Pre-submit manifest

`app.applications.presubmit_manifest` aggregates job identity, provider, capabilities,
resume/cover-letter artifacts + hashes + bindings, all mapped answers, unanswered required
fields, high-risk items still needing a decision, form + profile fingerprints,
approval/authorization state with live staleness recomputation, and the active blocker.

**Strictly read-only.** `ready_for_approval` *reports* what
`product_state.ready_for_approval()` already says plus the manifest's own blocking
observations; it introduces no new gate and nothing consults it to decide whether to submit.

**Privacy:** `as_dict()` redacts every prepared answer value by default; `include_values=True`
is opt-in for a human reviewing their own application. The CLI mirrors this
(`presubmit-manifest <job_id> [--show-values]`).

---

## 7. Confirmation detection

`app.applications.confirmation_parser` is now the **single source** of the success-phrase,
duplicate-application and confirmation-id tables (previously private constants inside the
Playwright-importing `browser_runtime`, so they could only be exercised through a live
browser). `browser_runtime` imports them; it keeps no parallel copy.

Three separate, non-overlapping concerns:

| Module | Question |
|---|---|
| `confirmation_parser` | what does this page's **text** say? |
| `confirmation_evidence` | how **strong** is that observation? |
| `browser_runtime` | supplies the real observation |

Ordering and semantics are unchanged: duplicate-application evidence is checked **first** and
returned distinctly (never folded into a fresh confirmation), and a trusted completed-action
phrase match is still required. Only `STRONG`/`MODERATE` may set an execution `APPLIED`.
`_check_confirmation_phrase_tables_disjoint` statically enforces the two tables stay disjoint.

---

## 8. Local fixtures

`tests/browser_fixtures.py` gained realistic, deterministic `greenhouse_like_*` and
`lever_like_*` `file://` pages — application form (standard fields, sponsorship radio group
whose question lives in the fieldset legend, optional cover letter, optional unknown
question), form-changed variant, CAPTCHA, login wall, expired posting, confirmation page,
and a cross-provider identity-mismatch pair.

Field names/labels are modeled on each provider's genuine shapes: Greenhouse's from the real
captured `?questions=true` payload (`first_name`/`resume`/`question_N`/`disability_status`),
Lever's from its live-verified rendered DOM (`name`/`urls[LinkedIn]`/`cards[...][fieldN]`) —
which is exactly why matching must key on **label text**, not a provider's field-name
convention.

No real employer is contacted by any test, and nothing is ever submitted anywhere.

---

## 9. Bugs this feature found and fixed

All four were **pre-existing** and surfaced by this feature's own tests:

1. **`invented_total_steps` fired on every real single-page session.**
   `_resolve_step_fields` persisted `total_steps_if_known` from `total_steps_hint`
   (a *guess*: 2 if a Next control is visible, else 1) alongside
   `step_confidence='UNKNOWN'` — precisely the invented total CLAUDE.md's Phase 11/12 rule
   forbids and the doctor check exists to catch. Now only a genuinely parsed EXACT
   "Step N of M" reading may persist a total; a previously-known genuine total is carried
   forward, a guess never introduced.

2. **The application doctor had never worked against PostgreSQL at all.**
   `_check_duplicate_active_execution` (and three siblings) used `HAVING n > 1` — a SELECT
   alias, which SQLite accepts and Postgres rejects — so `run_doctor()` aborted on its first
   grouped check. `_check_missing_answer_snapshot` additionally selected an ungrouped,
   unaggregated column. Fixed to `HAVING COUNT(*) > 1` and a `NOT EXISTS`. A regression test
   now runs **every** check individually under real Postgres.

3. **The confirmation-id regex captured bare English words.**
   "Application received" yielded `received`; "Application submitted" yielded `submitted`.
   That word would have been stored durably as a `confirmation_id` on the execution and its
   receipt **and** corroborated the evidence grade up from `MODERATE` to `STRONG` — a second
   independent signal invented out of nothing. A confirmation id must now contain at least
   one digit, and extraction scans all matches so a real id past an earlier false positive
   is still found.

4. **`"I do not want to answer"` matched no decline phrase.**
   The table held only the contracted `"i dont want to answer"` and the different-verb
   `"i do not wish to answer"`. `"I do not want to answer"` is the exact choice string on the
   real Greenhouse EEOC payload this project captured live — so a real Greenhouse demographic
   question had **no** decline option recognized.

---

## 10. What is deliberately still not supported

- **No real ATS auto-submit.** Not Greenhouse, not Lever, not any other real provider.
  Nothing in `browser_runtime` clicks a final submit control, and the static doctor check
  `_check_no_browser_auto_submit_capability` enforces that by scanning the module's public API.
- **No CAPTCHA / MFA / OTP / login / account-creation / anti-bot bypass.** Every one of these
  pauses the session with a durable, human-readable blocker.
- **Lever API-side form discovery.** Genuinely unavailable; reached via the browser instead.
- **Confirmation capture against a real submission.** Every provider's confirmation evidence
  is `FIXTURE_VERIFIED`. `ConfirmationCaptureLevel.LIVE_SUBMISSION_VERIFIED` is modeled so the
  vocabulary can express the distinction honestly, and is used by **no** row — earning it
  would require genuinely submitting to a real employer, which this project never does.
