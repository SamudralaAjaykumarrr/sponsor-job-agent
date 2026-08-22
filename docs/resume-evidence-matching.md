# Resume Evidence Matching

## Evidence graph (`app/resume_optimizer/evidence.py`)

`build_evidence_graph(profile: CandidateProfile) -> EvidenceGraph` reads ONLY the verified
candidate profile. For each skill in `profile.skills`:

- `DIRECT_VERIFIED` -- the skill is both listed AND tied to real evidence: it appears in some
  employment/project entry's `skills_used`, or in the text of a `verified_bullets` entry.
- `FAMILIAR_ONLY` -- the skill is listed but has zero supporting bullets/skills_used anywhere.
  A bare listed skill is never inflated to `DIRECT_VERIFIED` without real evidence behind it.

`responsibility_evidence` maps each `RESPONSIBILITY_SIGNALS` phrase (from
`app.resume_optimizer.jd_analysis`) to the verified bullets that actually mention it.
`domains` lists which `DOMAIN_SIGNALS` phrases appear anywhere in the candidate's verified
bullets/project descriptions/titles.

## Matching (`app/resume_optimizer/matching.py`)

`match_requirements(requirements, graph, profile) -> list[RequirementMatch]` produces one
`MATCHED`/`PARTIAL`/`TRANSFERABLE`/`MISSING`/`UNSUPPORTED` verdict per JD requirement:

| Category | Match logic |
|---|---|
| Skill-shaped (`LANGUAGE`, `FRAMEWORK`, `DATABASE`, `CLOUD`, `DEVOPS`, `MESSAGING`, `TESTING`, `SECURITY`, `ARCHITECTURE`, `FRONTEND`, `BACKEND`, `DATA_ML`, `TOOL`, `METHODOLOGY`) | `DIRECT_VERIFIED` evidence -> `MATCHED`; `FAMILIAR_ONLY` -> `PARTIAL`; else check transferable evidence (below); else `MISSING` |
| `RESPONSIBILITY` | A verified bullet mentions the same responsibility signal -> `MATCHED`; else `MISSING` |
| `EDUCATION` | A verified education entry meets or exceeds the required degree rank (PhD=3, Master's=2, Bachelor's=1) -> `MATCHED`; else `MISSING` |
| `CERTIFICATION` | Always `MISSING` -- `CandidateProfile` has no verified certifications field at all, so a certification requirement can structurally never be anything but honestly reported as missing (CLAUDE.md section 17) |
| `YEARS_EXPERIENCE` | `profile.standard_answers.years_of_experience >= required` -> `MATCHED`; less -> `PARTIAL` with the gap shown; `None` -> `MISSING` ("cannot compare"). Years are NEVER altered. |

## Transferable evidence safety (section 8)

`transferable_evidence_for_category()` looks for OTHER `DIRECT_VERIFIED` skills in the SAME
`RequirementCategory` as the missing item, and only when that category is in
`TRANSFERABLE_ELIGIBLE_CATEGORIES`:

```
TRANSFERABLE_ELIGIBLE_CATEGORIES = SKILL_CATEGORIES - {LANGUAGE, ARCHITECTURE, SECURITY}
```

`LANGUAGE`, `ARCHITECTURE`, and `SECURITY` are deliberately excluded -- claiming "Python
experience is transferable to a missing Go/Java requirement" is not a defensible truthful
framing the way "one REST framework's experience is transferable to another REST framework" is.
A missing language is always `MISSING`, matching CLAUDE.md acceptance scenario B ("JD asks
unsupported Go -> Go remains missing").

When a `TRANSFERABLE` match IS produced, its `explanation` string is always of the form:

> "No direct 'X' evidence; transferable experience via verified `<category>` work with
> `<other skills>` (never claimed as hands-on X)."

The resume content generator (`app/resume_optimizer/optimizer.py`) never inserts the missing
term itself into any resume field -- only the genuinely verified skills/bullets that back the
transferable claim.

## `resume_evidence_links` -- claim provenance (sections 6, 60-61)

Every `RequirementMatch` is persisted (`repo.save_evidence_links`) as one row per requirement
per resume variant: requirement text/category/priority, match status, evidence ids, and the
human-readable explanation. This is the data behind the job-detail page's "Claim provenance"
`<details>` panel and the `/api/jobs/{job_id}/resume-evidence` endpoint -- every resume
sentence/skill is traceable back to exactly which verified evidence justified including it, and
every `MISSING`/`UNSUPPORTED` item is shown honestly rather than hidden.

## Acceptance scenarios verified by `tests/test_resume_optimizer_matching.py` and
`tests/test_resume_optimizer_generation.py`

- A. JD asks Python/PostgreSQL/AWS with verified evidence -> `MATCHED`, included/prioritized.
- B. JD asks unsupported Go -> `MISSING`, never inserted into resume text.
- C. JD asks unsupported certification -> `MISSING`, never fabricated.
- D. JD asks 7 years, profile has 3 -> `PARTIAL` with gap shown; `standard_answers.years_of_experience`
  unchanged.
- E. JD asks Java; candidate has verified backend/REST-API responsibility evidence -> Java itself
  stays `MISSING` (LANGUAGE is not transferable-eligible), but `RESPONSIBILITY` matching
  (`"build REST APIs"`) independently shows `MATCHED` -- the honest way to represent "transferable
  backend experience exists" without a fabricated skill-level Java claim.
