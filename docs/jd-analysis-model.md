# JD Analysis Model

`app/resume_optimizer/jd_analysis.py::analyze_jd(job_title, description) -> JDAnalysisResult`

Pure function: JD title + description text in, a normalized requirements model out. It never
takes a candidate profile argument -- structurally guaranteed (`inspect.signature(analyze_jd)`
has exactly `job_title, description`), matching CLAUDE.md section 3.

## `JDRequirementItem`

Every extracted item carries:

- `text` -- the matched phrase as it appears (lowercased for skills, original case for
  education/certification labels)
- `normalized_value` -- lowercased key used for matching against candidate evidence
- `category` -- one of `RequirementCategory` (`LANGUAGE`, `FRAMEWORK`, `DATABASE`, `CLOUD`,
  `DEVOPS`, `MESSAGING`, `TESTING`, `SECURITY`, `ARCHITECTURE`, `FRONTEND`, `BACKEND`,
  `DATA_ML`, `TOOL`, `METHODOLOGY`, `RESPONSIBILITY`, `DOMAIN`, `EDUCATION`, `CERTIFICATION`,
  `YEARS_EXPERIENCE`, `TITLE`, `OTHER`)
- `priority` -- `REQUIRED` or `PREFERRED`
- `evidence_span` -- the surrounding JD text the extraction came from (auditability)
- `confidence` -- lower for conditional language
- `negated` / `conditional` -- see below

## Required vs preferred (section 4)

`_section_priority_for_offset()` tracks the most recent required-section marker
(`"required qualifications"`, `"required:"`, `"must have"`, ...) vs preferred-section marker
(`"preferred qualifications"`, `"preferred:"`, `"nice to have"`, `"a plus"`, ...) preceding a
match, defaulting to `REQUIRED` when no marker precedes it at all (a bare mid-paragraph mention
is treated conservatively, never silently dropped from coverage counting).

`_local_priority_override()` then checks a **clause-bounded** window (stops at the nearest
`.`/`;`/`:`, never bleeding into an adjacent sentence) immediately around the match for an
inline required/preferred phrase (`"is required"`, `"is a plus"`, `"mandatory"`, `"optional"`,
...) and overrides the section-based default when found -- a local, specific signal beats a
distant section header. This is what correctly classifies `"Bachelor's degree ... required"`
as `REQUIRED` even when it falls textually inside an earlier `"Preferred:"` section, and
`"AWS Certified Developer is a plus"` as `PREFERRED` even inside a `"Required:"` section.

A real bug this phase's own test-writing caught: the first version of the clause-boundary logic
used a fixed-character forward/backward window (not stopped at punctuation), which caused the
NEXT sentence's section header to leak into the current item's priority decision (e.g. `"...AWS.
Preferred: Kubernetes..."` flipped `aws` itself to `PREFERRED`). Fixed by bounding every local
window to `.`/`;` boundaries first, then capping at a max character count within that clause.

## Negation and conditional language (section 5)

`_is_negated()` and `_is_conditional()` share the same clause-bounded window logic as the
priority override (same real bug/fix: an early character-window-only version let `"Java is not
required. Python is required."` incorrectly mark `python` as negated too, because the fixed
60-character window reached backward into the PRIOR sentence's `"not required"` phrase). Both
now stop at sentence boundaries.

Negation patterns: `"not required"`, `"not necessary"`, `"no experience necessary"`, `"not
mandatory"`, `"is not required"`, `"not a requirement"`, `"without ... experience"`.
Conditional patterns (never dropped, marked `conditional=True` with reduced confidence instead):
`"case-by-case"`, `"may be considered"`, `"depending on"`, `"if available"`, `"where
applicable"`.

## Years, education, certification (sections 15-17)

- Years: `(\d+)\s*\+?\s*-\s*(\d+)\s*\+?\s*years` and `(\d+)\s*\+?\s*years`, negation-checked.
- Education: `PhD`, `Master's degree`, `Bachelor's degree`, `Computer Science degree` (bare
  `M.S.`/`B.S.` also recognized).
- Certification: bounded-word-count patterns (`{0,3}` following words, never an unbounded
  greedy match) plus a trailing-stopword trim (`_trim_cert_label`) and word-set-based dedup so
  `"AWS Certified Developer is a plus"` extracts as `"AWS Certified Developer"`, not
  `"AWS Certified Developer is a plus"` verbatim (a real over-greedy-regex bug this phase's own
  smoke test caught before any test file existed).

## Responsibilities, domain, sponsorship language, salary (sections 12-13, 18)

Fixed keyword-signal lists (`RESPONSIBILITY_SIGNALS`, `DOMAIN_SIGNALS`) checked as whole-word
matches. `sponsorship_language_present` and `salary_mentioned` are simple boolean detectors used
only for JD-analysis display -- they never influence `jobs.sponsorship_status` (that remains
`app.sponsorship.decision`'s exclusive responsibility, CLAUDE.md's durable Phase 7 rule).

## What this module deliberately does NOT do

- Never reads `CandidateProfile` -- see `app/resume_optimizer/evidence.py` and `matching.py`
  for that.
- Never fabricates a requirement that isn't actually in the text.
- Never claims a fixed, universal extraction accuracy number.
