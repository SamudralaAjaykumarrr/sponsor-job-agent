# Sponsorship Decision Engine

The single most important boundary in Phase 7. Read this before touching
`app/sponsorship/classifier.py` or `app/sponsorship/decision.py`.

## The hard invariant

> Historical sponsorship evidence helps answer *"is this employer worth
> prioritizing/reviewing?"*. It must **never** answer *"this specific
> current role definitely sponsors."*

Concretely:

- `NO_SPONSORSHIP` and `CONFIRMED_SPONSOR` are **always** decided by
  current-role evidence alone (the JD text, or explicit company policy tied
  to the current role). Historical evidence is attached only as extra
  explanatory `reasons` -- it never changes either of these two statuses.
- Historical evidence can **only ever** move `UNKNOWN` → `LIKELY_SPONSOR`.
  It can never produce `CONFIRMED_SPONSOR`, and it never overrides
  `NO_SPONSORSHIP`.
- A conflict (positive **and** negative sponsorship language in the same
  JD) always resolves to `LIKELY_SPONSOR` (review-only, never auto-apply) --
  never a hard skip, never `CONFIRMED`.

## Two layers, deliberately separated

### Layer 1: `app/sponsorship/classifier.py` -- current-role only

`classify_sponsorship_detailed(description, company)` reads **only** the JD
text and the local known-sponsors reference list (`data/known_h1b_sponsors.json`).
It **never imports** `app.sponsorship.evidence`/`profile`/`decision` --
this is the same boundary CLAUDE.md's Phase 6 rule established for
`app.sponsorship.classifier`, preserved exactly as originally worded.
`classify_sponsorship()` (the pre-Phase-7 two-tuple signature) is an
unchanged-behavior wrapper around it, still used wherever pre-Phase-7 code
called it directly.

Deterministic rule order inside this layer:

1. **Both positive and negative patterns match** → `CONFLICT`:
   `LIKELY_SPONSOR`, `conflict=True`, blocking reason explains both spans
   found. Never a hard skip, never CONFIRMED.
2. **Negative pattern matches (no positive)** → `NO_SPONSORSHIP`. Dominant
   over conditional language too -- safety first.
3. **Positive pattern matches (no negative)** → `CONFIRMED_SPONSOR`.
4. **Conditional/case-by-case pattern matches only** ("may sponsor",
   "case-by-case", "certain visa types only", "sponsor exceptional
   candidates") → `LIKELY_SPONSOR`, `conditional=True`. Never `CONFIRMED`
   per CLAUDE.md section 18.
5. **Nothing matches** → local known-sponsors reference list lookup (exact/
   substring match on employer name) → `LIKELY_SPONSOR` if found, else
   `UNKNOWN`.

Negation-safety patterns added in Phase 7 (CLAUDE.md section 17), each with
a dedicated test in `tests/test_sponsorship_decision.py`: "we do not offer
visa sponsorship", "applicants requiring sponsorship will not be
considered", "we have historically sponsored employees, but this role does
not support sponsorship", "US citizens only", "permanent work authorization
required", "must be authorized to work without sponsorship".

### Layer 2: `app/sponsorship/decision.py` -- the ONLY place history is blended in

`decide_sponsorship(title, company, description, location_state)` is the
sanctioned integration point CLAUDE.md Phase 7 asks for. It:

1. Calls `classify_sponsorship_detailed()` for the current-role result.
2. If the result is `NO_SPONSORSHIP` or `CONFIRMED_SPONSOR` → returned as-is.
   Historical context is looked up and appended to `reasons` purely for
   display; the status is untouched.
3. If the result is `LIKELY_SPONSOR` (conflict, conditional, or local-list
   match) → returned as-is, with historical context appended to `reasons`.
4. If the result is `UNKNOWN` → this is the **only** branch where history
   can change the outcome:
   - Resolve employer identity (`app.sponsorship.identity.resolve_company`).
   - No match / ambiguous → stays `UNKNOWN`.
   - Match found → fetch/compute the cached `EmployerProfile`
     (`app.sponsorship.profile`), compute role similarity between the
     current title and every recent occupation title on file
     (`app.sponsorship.similarity.role_similarity`, keeping the strongest
     match), compute location similarity against recent filing states.
   - **Deterministic threshold**: `historical_strength == STRONG_RECENT`
     **and** `role_similarity in {STRONG, MODERATE}` → upgrade to
     `LIKELY_SPONSOR`. Anything weaker (SOME/OLD/NONE strength, or NONE/WEAK
     role similarity) stays `UNKNOWN`. This is CLAUDE.md section 43's
     required behavior for examples C and D: strong recent technical
     history with a matching role can become `LIKELY_SPONSOR`; old,
     non-technical, or unrelated-role history never does, and neither case
     can ever reach `CONFIRMED_SPONSOR`.

`app.sponsorship.classifier` is never imported by `app.sponsorship.evidence`/
`profile`/`identity`, and neither of those is imported by `classifier.py` --
the dependency direction only ever flows `decision.py` → `classifier.py` +
`profile.py`/`identity.py`, never the reverse.

## Decision audit trail (`sponsorship_decisions`)

`persist_decision(job_id, title, company, description, location_state)`
computes a decision and writes a **new, append-only, versioned** row **only
if** the JD fingerprint (SHA-256 of normalized `title|company|description`)
or the classifier version differs from the last recorded decision for this
job. Re-running on unchanged input is a no-op read (no new row, no wasted
write). A JD edit always gets its own row -- prior decisions are never
overwritten, so `list_decision_history(job_id)` shows the full
reclassification history with `decision_version` strictly increasing.

`app/pipeline.py::analyze_job()` calls `persist_decision()` (not
`classify_sponsorship()` directly) as its sponsorship step, so every
pipeline-analyzed job gets an audited decision automatically.

## JD-change detection and reanalysis (`app/pipeline.py::reanalyze_job`)

`reanalyze_job(job_id, new_title=None, new_company=None, new_description=None)`:

- No-ops if none of the supplied text actually differs from what's stored.
- If the job is in a **terminal, human-driven state**
  (`APPLIED`/`INTERVIEW`/`REJECTED`), the new decision is still computed
  and recorded for audit history, but `application_state` is **left
  untouched** -- a human already acted on this job, and a later JD edit must
  never silently move it (CLAUDE.md Phase 7 section 23, and the explicit
  scenario in `test_terminal_application_state_not_silently_moved_by_jd_change`).
- Otherwise, it re-runs the full `analyze_job` gate sequence (target-role,
  sponsorship, seniority, compensation, match score) and the same
  ASSIST-mode progression `ingest_and_process` uses
  (`app.pipeline._progress_after_analysis`), so a JD that flips from silent
  to explicit sponsorship reaches `READY_TO_APPLY`, and a JD that flips
  from positive to explicit no-sponsorship is hard-skipped -- exactly
  CLAUDE.md's scenarios 7 and 8.

## Conflict handling summary

| Current-role signal | Historical evidence | Result |
|---|---|---|
| Explicit NO | (anything, including strong history) | `NO_SPONSORSHIP` -- history never overrides |
| Explicit YES | (anything, including none) | `CONFIRMED_SPONSOR` -- history never overrides |
| Both YES and NO present | n/a | `LIKELY_SPONSOR`, `conflict=True`, review required |
| Conditional/case-by-case | n/a | `LIKELY_SPONSOR`, `conditional=True`, review required |
| Silent | Strong recent + matching role | `LIKELY_SPONSOR` |
| Silent | Weak/old/unrelated/none | `UNKNOWN` |
| Silent | Local known-sponsors list match | `LIKELY_SPONSOR` (pre-Phase-7 behavior, unchanged) |

## Explanation fields exposed everywhere a decision is shown

`SponsorshipDecision`: `status`, `evidence_text`, `reasons[]`,
`blocking_reason`, `conflict`, `current_job_evidence[]`,
`historical_evidence_summary{}`, `decision_version`, `classifier_version`,
`jd_fingerprint`. Never empty for any of the four statuses
(`tests/test_sponsorship_decision.py` covers all four; see also section 46
"Decision explainability" in the acceptance test matrix).
