# Sponsorship Classification Rules

Applied in this order against the job description + company name:

1. **NO_SPONSORSHIP** (hard skip) if JD text matches any no-sponsorship phrase, e.g.:
   "no sponsorship", "not able to sponsor", "unable to sponsor", "will not sponsor",
   "cannot sponsor", "without sponsorship now or in the future", "must not require sponsorship",
   "no visa sponsorship", "not sponsoring".
2. **CONFIRMED_SPONSOR** if JD text matches an explicit sponsorship-available phrase, e.g.:
   "sponsorship available", "will sponsor", "we sponsor", "h-1b sponsorship provided",
   "visa sponsorship available", "open to sponsorship", "sponsors work visas".
3. **LIKELY_SPONSOR** if the employer name matches an entry in the bundled local reference
   list of past H-1B filers (`data/known_h1b_sponsors.json`) and neither of the above matched.
   This is explicitly review-only — historical filing is not proof a specific role sponsors.
4. **UNKNOWN** otherwise — not enough evidence. Per spec: do not apply.

Downstream effect (`pipeline.py` / `scoring/scorer.py`):

| Status            | Effect                                  |
|-------------------|------------------------------------------|
| CONFIRMED_SPONSOR  | eligible — full pipeline, application_state = READY_TO_APPLY |
| LIKELY_SPONSOR     | review only — package still generated, application_state = REVIEW_REQUIRED, never auto-submitted |
| UNKNOWN            | analyzed, not progressed — do not apply (application_state = ANALYZED) |
| NO_SPONSORSHIP     | hard skip — no further processing, application_state = SKIPPED_NO_SPONSORSHIP |

This applies identically whether the job arrived via manual JD paste or the autonomous
discovery agent (`docs/autonomous-agent.md`) — sponsorship classification and its downstream
state are not source-dependent.

Priority ordering (highest first): Remote+Confirmed > Remote+Likely > Hybrid+Confirmed >
Hybrid+Likely > Onsite+Confirmed > Onsite+Likely. Any job classified NO_SPONSORSHIP is
skipped regardless of remote status.
