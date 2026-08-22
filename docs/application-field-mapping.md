# Application Field Mapping

## The generic field schema

`app/applications/models.py::ApplicationField` and `FieldCategory` implement
CLAUDE.md Phase 8 section 8's 19 categories (CONTACT, WORK_AUTHORIZATION,
SPONSORSHIP, EMPLOYMENT, EDUCATION, EXPERIENCE, SKILLS, LOCATION,
RELOCATION, SALARY, NOTICE_PERIOD, PROJECTS, DEMOGRAPHICS,
VOLUNTARY_DISCLOSURE, LEGAL_ATTESTATION, CUSTOM_TEXT, FILE_UPLOAD, CONSENT,
SIGNATURE).

`app/applications/schema.py::build_application_fields(profile, ...)` maps
`candidate_data/profile.json` (the ONLY candidate truth source) into this
schema. A profile field left as `NEEDS_USER_INPUT`/`None` produces an
`ApplicationField` with `needs_user_input=True`, `auto_fill_allowed=False` —
never a guessed value. Legal/attestation fields (criminal history, security
clearance, export control, non-compete, government employment, conflict of
interest, background check consent, drug testing, signature) are **not
present anywhere in the candidate profile schema by design** — they are
always `needs_user_input=True`.

## The matching engine

`app/applications/mapping.py::match_field(label, name)` matches one real ATS
form field against the canonical field vocabulary:

| Confidence | How it matches | Auto-fill allowed? |
|---|---|---|
| EXACT | normalized label exactly equals a registered alias | yes, if source is verified |
| HIGH | normalized `name`/`id` attribute exactly equals a registered alias | yes, if source is verified |
| MEDIUM | every token of some alias is a subset of the label's tokens | only for non-sensitive categories, reviewed |
| LOW | nothing matched | never — `needs_user_input=True` |

**The MEDIUM token-overlap fallback never applies to LEGAL_ATTESTATION,
DEMOGRAPHICS, VOLUNTARY_DISCLOSURE, CONSENT, or SIGNATURE fields**
(`app.applications.mapping._STRICT_FIELD_IDS`) — those can only ever match
via an exact registered alias. This is CLAUDE.md Phase 8 section 14's "do
not use unsafe fuzzy matching for legal fields" rule, enforced structurally
rather than by convention.

## Sponsorship answers

The candidate's real, stated answer (`work_authorization.requires_sponsorship`)
is always used truthfully — never hidden or misrepresented, per CLAUDE.md
Phase 8 section 10. If the profile hasn't stated an answer, the field is
`needs_user_input=True` and the form stops at `NEEDS_USER_ACTION` rather than
guessing.

## Demographic / voluntary questions

Never inferred. If the candidate profile has a real stated value, it's used
as-is. If not, and the real form offers a "decline to self-identify"-shaped
choice (checked via `app.applications.schema.DECLINE_TO_SELF_IDENTIFY_PHRASES`
against the form's own offered choice labels — never guessed independently
of what the form actually offers), that choice is selected. Otherwise the
field is left unresolved and the executor stops at `NEEDS_USER_ACTION` with
`PolicyReason.UNKNOWN_DEMOGRAPHIC_QUESTION`.

## Legal / attestation questions

Always `needs_user_input=True` (see above) — any form field matching one of
these categories via the mapping engine's EXACT-alias path is guaranteed
unresolved, and `ApplicationProvider.validate()` reports
`PolicyReason.UNKNOWN_LEGAL_QUESTION` for it. There is no code path that can
ever fill one of these fields from an inference.

## Salary

Uses `preferences.salary_min_usd` when present and truthful; if the field
can't be safely/exactly determined, it's left `needs_user_input=True`. Never
fabricates a "current salary" — the candidate profile schema has no such
field.

## Confidence thresholds in practice

`app/applications/schema.py::_field()` caps confidence at `HIGH` (never
`EXACT`) for any `SENSITIVE_CATEGORIES` field even when the candidate did
provide a real answer — so a sensitive field is always flagged for review
attention even when it auto-fills correctly.
