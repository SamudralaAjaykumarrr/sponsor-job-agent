# Confirmation Evidence Strength (Phase 13)

CLAUDE.md Phase 13 sections 49-52.

## What already existed (unchanged)

`app.applications.browser_runtime._SUCCESS_PHRASES` is a curated set of
specific, affirmative, completed-action phrases ("thank you for applying",
"application submitted", ...). Only a genuine phrase match sets
`ConfirmationOutcome.confirmed = True` — this was already true before
Phase 13 and remains true. `_DUPLICATE_APPLICATION_PHRASES` ("you have
already applied", ...) is checked first and routes to a distinct
`already_applied=True` outcome, never folded into a fresh confirmation.

## What's new: a graded strength model

`app.applications.confirmation_evidence.ConfirmationEvidenceStrength`:
`STRONG`, `MODERATE`, `WEAK`, `NONE`.

`classify_confirmation_evidence(phrase_matched, confirmation_id,
current_url)`:

- **STRONG**: a trusted phrase matched AND a corroborating signal
  (confirmation id extracted, or the URL itself looks confirmation-shaped)
  is also present.
- **MODERATE**: a trusted phrase matched alone. Still sufficient to
  confirm — the existing curated phrase table is already a strong single
  signal.
- **WEAK**: only a confirmation id or confirmation-shaped URL was observed
  WITHOUT a trusted phrase. Modeled for completeness (a future provider-
  specific pattern might reach this path) but not reachable from today's
  phrase-gated capture flow, and **never** sufficient to confirm on its
  own.
- **NONE**: nothing observed.

`ConfirmationGrade.confirms()` returns `True` only for `STRONG`/`MODERATE`
— this is the actual gate `_do_capture_confirmation` uses, so the
functional behavior is unchanged from before Phase 13 (a phrase match
still confirms); what's new is that the grade is now recorded
(`browser_assist_sessions.confirmation_evidence_strength`) rather than
implicit.

## False-positive rejection (CLAUDE.md section 50)

These must never, by themselves, produce `phrase_matched=True` (enforced by
the existing, unchanged `_SUCCESS_PHRASES` table, tested directly in
`tests/test_confirmation_evidence.py`):

- "Submit successfully to continue"
- "Your application will be received after..."
- "Success stories"
- "Application confirmation will be emailed"

## Doctor coverage

`applied_with_weak_confirmation` — a `CONFIRMED` browser-assist session
must carry `STRONG` or `MODERATE` evidence, never `WEAK`/`NONE`/unset.
This is the browser-assist path's evidence-STRENGTH-aware counterpart to
the executor path's existing `_check_false_confirmation_evidence`.

## Metrics

`confirmation_strong_total`, `confirmation_unknown_total`
(`collect_phase13()`).

## Provider-specific patterns

CLAUDE.md section 52 allows adding provider-specific confirmation patterns
where genuinely observed. None were added this phase — every real
provider validated (Greenhouse, Workable, SmartRecruiters, Lever, Ashby)
was only ever reached up to a real, un-clicked final-submit control (this
project never clicks it), so no genuine POST-submission confirmation page
text has actually been observed for any of them yet. Documented here as
`NOT_TESTED` rather than fabricated.
