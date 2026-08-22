# ATS Parse Validation

`app/resume_optimizer/ats_parse.py`

## What this validates (and what it does not)

This module never depends on, or claims to reproduce, any real ATS vendor's proprietary parsing
or scoring engine (CLAUDE.md section 29). It only checks that OUR generated DOCX/PDF/TXT
artifacts extract cleanly via widely-used, general-purpose libraries (`python-docx` for DOCX,
`pypdf` for PDF, plain file read for TXT) -- the same class of extraction a real ATS's resume
parser is likely to perform, but never presented as equivalent to one.

## `PASS` / `WARN` / `FAIL`

For each format, `_expected_terms(resume)` builds a list of (label, text) pairs that MUST be
findable in the extracted text: candidate name, contact email, "Summary"/"Skills" section
headers, the first employer + title, the first education school, the first project name.

- 0 missing terms, sensible reading order -> `PASS`
- 1-2 missing terms, or the candidate's name appearing AFTER the skills section in extracted
  order -> `WARN`
- 3+ missing terms, or extraction failing entirely (corrupt file, unreadable, empty text) ->
  `FAIL`

`ATSParseReport.overall` is the worst of the three format results (`FAIL` > `WARN` > `PASS`).

## DOCX validation (section 30)

Extracts every paragraph's text plus every table cell's text (the resume template itself never
uses tables for layout, but the check covers them defensively). Checked: text extraction
succeeds at all, heading/section text present, employer/title/dates present, education present,
contact info present.

## PDF validation (section 31)

Uses `pypdf.PdfReader.extract_text()` per page. If reading order comes out garbled (a known risk
of some PDF generation approaches), the name-before-skills ordering check catches it and reports
`WARN`/`FAIL` rather than silently passing a poorly-ordered document.

## TXT validation (section 32)

The plain-text artifact is the ground-truth debugging view -- if TXT validation fails, the
underlying `ResumeContent` itself is likely missing required data (e.g. `NEEDS_USER_INPUT` in a
required field), not a parsing artifact.

## Integration (`app/resume_optimizer/optimizer.py`)

Every `optimize_resume()` call runs `ats_parse.validate_all()` immediately after writing the
DOCX/PDF/TXT artifacts. A variant whose `ats_parseability.overall == "FAIL"` is marked
`ATS_PARSE_FAILED` (never silently served as `READY`) -- checked BEFORE the `READY` status is
assigned, alongside the (higher-priority) claim-check gate:

```
if claim_violations:            status = CLAIM_CHECK_FAILED
elif ats_report.overall == FAIL: status = ATS_PARSE_FAILED
else:                             status = READY
```

A `WARN` result does not block `READY` (a resume with e.g. an unusual but still-extractable
reading order is still usable) but is surfaced in the resume's `warnings` list and displayed on
the job-detail page.

## Fixtures (section 73)

`tests/test_resume_optimizer_ats_parse.py` builds a deterministic `ResumeContent` fixture (fixed
name/skills/experience/project/education) and validates: full-pass extraction, per-field
presence, a missing-file `FAIL`, an empty-file `FAIL`, and a partial-content `WARN`/`FAIL`.
`tests/test_resume_optimizer_generation.py` additionally validates real, optimizer-generated
DOCX/PDF/TXT artifacts end-to-end for both a strong-fit and a low-fit JD.
