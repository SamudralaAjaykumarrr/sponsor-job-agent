# Application Provider Interface

`app/applications/provider.py`'s `ApplicationProvider` is a separate
interface from `app/providers/base.py`'s `JobProvider` (discovery). A
provider can be fully supported for discovery and completely unsupported for
application — this is true today for Lever.

```
detect_application(job) -> bool
discover_form(job) -> FormSnapshot | None
map_fields(form, application_fields) -> MappingResult
fill_draft(form, mapping) -> DraftResult
validate(job, form, draft) -> ValidationResult
submit(job, form, draft) -> SubmitResult          # only called when validate() says PERMITTED_AUTO
verify_confirmation(submit_result) -> ConfirmationResult
check_submission_status(job, execution) -> ConfirmationResult | None   # Phase 9, optional
check_job_still_active(job) -> bool | None                              # Phase 9, optional
```

The two Phase 9 hooks are both optional with safe defaults (`None` — "not
checkable"/"not supported") on the base class. `check_submission_status()`
is used only by `app.applications.reconcile_worker` (see
`docs/application-reconciliation.md`); `check_job_still_active()` is used
only by `executor.process_execution()`'s pre-submission revalidation (see
`docs/application-worker-architecture.md`). Neither is required for a
provider to be otherwise fully functional — a provider that doesn't
implement them simply participates less in those two specific safety
mechanisms, never less safely.

## Capability matrix

See `docs/application-provider-capabilities.md` for the full, current,
generated table (`python -m app.applications.cli capability-matrix` /
`GET /applications/capability-matrix`). Summary as of Phase 9: `mock_ats`
is the only provider with `submission_supported=True` and
`confirmation_recheck_supported=True`; `greenhouse` has live-verified form
discovery but stays `ASSIST_ONLY` for submission; `lever` and every other
ATS this project discovers jobs from are `UNSUPPORTED` for application
(discoverability was never treated as proof of application support).

`GET /providers` doesn't show the application matrix (that's the Phase 3-7
discovery matrix); the application matrix is served at
`GET /api/applications/metrics` (aggregate),
`GET /applications/capability-matrix` (full table), and via
`app.applications.provider_registry.all_application_capabilities()` /
`app.applications.capability_matrix.build_matrix()`.

## Greenhouse: what was actually live-verified

During this phase's development, `https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}?questions=true`
(the officially documented public Job Board API's `questions` parameter —
https://developers.greenhouse.io/job-board.html) was fetched against a real
posting and confirmed to return genuine structured application-question
fields: `label`, `required`, and a `fields` array with `name`/`type`
(`input_text`/`input_file`/`textarea`/`multi_value_single_select`) and
`values` (choice label/value pairs) — including the standard EEOC
demographic questions (disability/veteran/race, each offering a
"decline to self-identify"-equivalent choice) and, on the specific posting
checked, an explicit sponsorship question. This is what
`GreenhouseApplicationProvider.discover_form()` parses.

Submission is intentionally **not** implemented. Greenhouse's actual "Apply"
action goes through the job board's own hosted, client-rendered form flow —
not a documented public API contract for third-party programmatic
submission. Automating it would mean reverse-engineering an undocumented
interface, which CLAUDE.md's safety rules explicitly rule out
("If Greenhouse submission requires interfaces that should not be automated
without explicit permission: mark ASSIST_ONLY"). So
`capabilities.submission_supported = False` and every draft this adapter
prepares stops at `ASSIST_ONLY`, with the fully-mapped, fully-filled draft
preserved for the candidate to submit manually.

## Lever: what was actually live-verified

`https://api.lever.co/v0/postings/{site}?mode=json` was fetched for a real
public Lever account and its posting objects were inspected directly: the
only application-relevant fields present are `hostedUrl`/`applyUrl` — no
structured question/field schema exists anywhere in the public API. Rather
than hardcode a guessed "typical Lever form" template (which risks silently
going stale or simply being wrong), `LeverApplicationProvider` honestly
reports `form_discovery_supported = False` and only ever hands back the
known apply URL.

## Adding a new adapter

1. Confirm (with an actual request, in a test or scratch script — never
   guessed) whether the ATS exposes a public, documented, unauthenticated
   endpoint returning the application form's field structure. If not, stop
   here and use `GenericAssistOnlyProvider`'s pattern (apply-URL only).
2. If yes, implement `discover_form()` against that real endpoint, with a
   corresponding fixture-based test (see
   `tests/test_applications_providers_greenhouse.py`) — no live network call
   in the normal `pytest` run.
3. `map_fields()`/`fill_draft()` should delegate to
   `app.applications.mapping.match_field()` — do not reimplement matching.
4. `submit()` may only be implemented, and `capabilities.submission_supported`
   set `True`, once genuine, tested, explicitly-permitted submission
   automation exists. Until then leave the base class's refusal in place.
