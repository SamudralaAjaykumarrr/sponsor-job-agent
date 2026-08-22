# Real ATS Validation (Phase 10)

Bounded, read-only, low-volume: **one real posting opened per provider**, via
`scripts/phase10_live_validation.py`, on 2026-08-22. No submission was ever attempted. Every
provider's section either reports what was genuinely observed, or honestly `NOT RUN` with the
reason — never a fabricated result.

## Method

For each provider: fetch one real posting via its public discovery API (already used,
unauthenticated, by the Phase 3 discovery connectors), open its real candidate-facing
application URL in headless Chromium, and run the exact
`app.applications.browser_runtime._detect_fields()`/`_detect_button()` code the product uses —
never a second, separate probing heuristic.

## Results

| Provider | Company/tenant | Fields detected | Upload | Submit button | Login wall | CAPTCHA | Result |
|---|---|---|---|---|---|---|---|
| Greenhouse | GitLab (`gitlab` board) | 24 | yes | yes (not clicked) | no | **yes, real** | LIVE_FORM_VERIFIED |
| Lever | Lever's own demo (`leverdemo`) | 22 | yes | yes (not clicked) | no | **yes, real** | LIVE_FORM_VERIFIED |
| Ashby | Ashby's own board (`ashby`) | 28 | yes | yes (not clicked) | no | **yes, real** | LIVE_FORM_VERIFIED |
| SmartRecruiters | SmartRecruiters' own board | 1 (share-link widget only) | no | no | no | no | NOT_TESTED (see below) |
| Workday | Workday's own dogfood tenant | — | — | — | — | — | NOT RUN (tenant offline) |
| Workable | (none known) | — | — | — | — | — | NOT RUN (no known real tenant) |

### Greenhouse — full detail

Real posting on GitLab's public Greenhouse board. 24 fields including First/Last Name, Email,
Phone, Country, LinkedIn, two attachment/resume upload controls, a free-response question, and
a genuine `"Will you now or in the future require sponsorship for a visa to remain in your
current location?*"` question — exactly the kind of field this project's field-mapping engine
(`app.applications.mapping`) is built to recognize. A real CAPTCHA widget was present and
correctly detected; a real submit button was located and never clicked. Final URL stayed on
`job-boards.greenhouse.io` (domain allowlist matched).

### Lever — full detail

Real posting on Lever's own public demo account. 22 fields including a resume upload, full
name/email/phone, LinkedIn/GitHub/portfolio/video-link URLs, and EEOC-style gender/race
demographic questions with a "Decline to self-identify" option each. A real CAPTCHA widget was
present and correctly detected. This is the same real form the Phase 8
`providers_lever.py` adapter documented as having *no* structured public API schema — the
browser engine reaches it anyway by reading the rendered DOM directly, which is exactly the gap
browser-assist exists to close for a provider whose network API doesn't expose one.

### Ashby — full detail

Real posting on Ashby's own public careers board (Ashby uses its own product to hire). 28
fields including free-response interview-style questions and demographic self-identification
choices (age range, gender, race/ethnicity options). A real CAPTCHA widget was present and
correctly detected.

### SmartRecruiters — full detail, honestly incomplete

The postings-list API's candidate-facing URL (`jobs.smartrecruiters.com/SmartRecruiters/<id>`,
confirmed live at HTTP 200) resolved to a **job-description landing page**, not the application
form directly — only a "copy link to share (WeChat)" control was detected. SmartRecruiters
apparently gates the real form behind a further "Apply"/"Apply Now" click this phase's bounded,
one-page-per-provider validation policy did not follow. Marked `NOT_TESTED` at the form level
rather than inflated to `LIVE_FORM_VERIFIED` — the landing page itself was safely opened and its
navigation/domain behavior observed, but the actual form was not reached. See "Recommended Phase
11" in `docs/phase10-real-ats-assist.md`.

### Workday — offline this session

The Phase 3 dogfood tenant (`workday.wd5.myworkdayjobs.com/Workday`) now redirects its CXS API
to `community.workday.com/maintenance-page` — genuinely unavailable as of this check, not a code
defect. No substitute tenant was guessed. See `docs/workday-application-assist.md`.

### Workable — no known real tenant

Phase 3's own dogfooding attempt against guessed tenant names never resolved to a real account
(documented in `docs/acceptance_verification.md`). Not fabricated here either.

## Rate/volume safety

Six total network requests plus six total browser page-opens across the entire validation run —
no crawling, no pagination beyond the first page of results, no repeated polling. Every provider
section either ran exactly once or reported `NOT RUN` immediately.

## Updating this file

Re-run `python scripts/phase10_live_validation.py` and update this file (and
`app/applications/browser_capability_matrix.py`'s corresponding row) only from a fresh,
genuinely-observed result — never bump a verification level from memory or assumption.
