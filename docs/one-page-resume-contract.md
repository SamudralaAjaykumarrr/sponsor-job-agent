# One-Page Resume Hard Output Contract

Every automatically-generated, job-specific resume must render as exactly
one PDF page. This is enforced code, not a style guideline: `app.resume_
optimizer.one_page.enforce_one_page()`, wired into `app.resume_optimizer.
optimizer.optimize_resume()`.

## Structural reference

Single-column, ATS-safe, one stable layout tailored per JD in *content*
only (never cosmetically per company — see "Role/company-aware emphasis"
below):

```
NAME + CONTACT
TARGET ROLE / SHORT SUMMARY
SKILLS
PROFESSIONAL EXPERIENCE
EDUCATION
```

No graphics, no text boxes, no multi-column layout, no hidden text — the
existing `app.resume.pdf_writer`/`app.resume.docx_writer` (ReportLab /
python-docx) never produced any of those; this feature only adds bounded
compression on top of that same simple, parseable structure.

## How page count is measured

`app.resume.pdf_writer.count_pdf_pages()` — `len(pypdf.PdfReader(path).pages)`
— the same `pypdf` dependency `app.resume_optimizer.ats_parse` already uses
for text extraction. No new dependency.

## The bounded compression ladder

After a normal render, if the PDF is more than one page,
`enforce_one_page()` applies, in order, until one page is reached or
`ONE_PAGE_MAX_COMPRESSION_STEPS` (default 8) is exhausted:

1. **Remove the single lowest-relevance optional bullet** across every
   experience/project entry (never an entry's last remaining bullet — every
   included role/project always keeps at least one piece of evidence).
   "Relevance" is JD-overlap with the skills the optimizer already promoted
   to the front of `skills_ordered` — so a bullet with zero overlap is
   always removed before one with any overlap, protecting required-evidence
   bullets until genuinely nothing else is left to trim.
2. **Remove the single weakest whole project entry** (projects are already
   an optional inclusion — CLAUDE.md "project inclusion if valuable" — so
   this is a legitimate second-tier step once bullet-trimming alone isn't
   enough).
3. **Remove the lowest-priority optional skill** from the tail of
   `skills_ordered` — never a JD-matched/priority skill.
4. **Shorten the summary** — drop a trailing clause (e.g. the "; verified
   strengths include ..." half) rather than the whole sentence.
5. **Reduce spacing/typography** — margins, heading sizes, and body font
   size (via `write_pdf`/`write_docx`'s `compression_level` parameter) scale
   down gradually, **never below `ONE_PAGE_MIN_FONT_SIZE`** (default 9.5pt).
6. Re-render, repeat.

If one page still cannot be achieved within the bound, the result is an
honest `ResumeVariantStatus.REVIEW_REQUIRED` — **never** a smaller-than-
readable render passed off as success. See
`tests/test_one_page_resume.py::test_pathological_resume_gives_up_honestly`.

## Why "shorten a verbose bullet" isn't a literal text-edit step

`app.resume.claim_checker.check_resume_claims()` — CLAUDE.md's single,
**unmodified** truthfulness firewall — requires every resume bullet to be an
*exact* string match against the candidate's `verified_bullets`. Truncating
or rewording a bullet's characters would turn it into text the claim checker
correctly rejects as unverified, and CLAUDE.md Phase 14 durable rules
forbid modifying, bypassing, or adding a looser parallel check to the claim
checker for any reason.

So this ladder only ever **removes** whole (already-verified) content,
regenerates the summary (which is free text, never claim-checked), or
adjusts pure rendering — it never rewrites a verified claim's wording. This
is a deliberate, documented adaptation of the general "shorten a bullet"
idea to this codebase's stricter, non-negotiable truthfulness invariant.

## Truthfulness ordering

The claim checker runs on the **full, uncompressed** resume first — since
compression only removes/shortens content and never adds any, if the full
resume is truthful, every compressed subset it might produce is too. A
`CLAIM_CHECK_FAILED` result always takes priority over a one-page-overflow
result in `optimize_resume()`'s status decision.

## Content tailoring per job, never fabrication

Per job, `optimize_resume()` generates a different variant linked to
`job_id` + `company` + `provider_job_id`/requisition + JD fingerprint +
candidate profile version + optimizer version + artifact hash
(`resume_variants` table, unchanged Phase 14 identity model). It tailors
summary, skill ordering, experience bullet *selection* (never wording,
which is fixed verified text), project inclusion, and section density.
It never alters employer, title, dates, education, verified years, or
verified metrics.

## Role/company-aware emphasis, not cosmetic branding

`app.resume_optimizer.jd_analysis`/`app.resume_optimizer.matching` (Phase 14,
unchanged) already select and order verified skills/bullets by domain
signal — infrastructure/platform JDs surface distributed-systems/containers/
CI/CD evidence first, payments/fintech JDs surface APIs/SQL/reconciliation
evidence, QA/SDET JDs surface testing/CI evidence, and so on, always from
the *same* verified profile. The one-page contract adds nothing new here —
it only ensures whatever the optimizer selected still renders as one page.
The visual layout never changes by company; only which verified content
appears, and how much of it, does.

## Overflow acceptance scenario

An intentionally verbose synthetic profile (many employers, many long
bullets each) first renders at several pages; the compression loop then
brings a realistically-dense (but not absurd) resume down to one page, while
a genuinely excessive one gives up honestly at `REVIEW_REQUIRED` rather than
producing an unreadable page. See `tests/test_one_page_resume.py`.

## Dashboard visibility

Every job row's Resume column shows a `1 PAGE ✓` badge when the current
variant is `READY` with `page_count == 1`, or a `REVIEW_REQUIRED` badge
otherwise. The job detail page additionally shows the page count, how many
compression steps were applied, and the full compression log (which steps
were taken, in order) in a collapsible section, plus whether this variant
was promoted to be the job's primary (application-ready) resume.

## Application-execution linkage

`app.agent.orchestrator._run_resume_stage()` promotes a variant onto
`jobs.resume_docx_path`/`resume_pdf_path`/`resume_txt_path`/
`resume_jd_fingerprint`/`promoted_resume_variant_id` **only** when it is
`READY` with `page_count == 1` — a `REVIEW_REQUIRED` overflow result is
never promoted, so the application executor can never pick up a multi-page
resume through the automatic pipeline. `app.applications.executor.
_verify_resume_artifact()` verifies the artifact's path still belongs to
this job and its hash hasn't drifted immediately before every use — this
check was broadened during this feature's own testing to recognize the
optimizer's nested `output/<job_id>/optimized/<variant_id>/` path shape
alongside the legacy flat `output/<job_id>/` one (a real integration gap
this feature's live testing caught — see `app/applications/executor.py`'s
docstring on `_verify_resume_artifact`).

## Config

| Variable | Default | Meaning |
|---|---|---|
| `ONE_PAGE_RESUME_REQUIRED` | `true` | Documents the contract is active (informational; the code always enforces it when generating through `optimize_resume()`) |
| `ONE_PAGE_MIN_FONT_SIZE` | 9.5 | Never shrink body font below this |
| `ONE_PAGE_MAX_COMPRESSION_STEPS` | 8 | Bounded ladder length before an honest `REVIEW_REQUIRED` |
