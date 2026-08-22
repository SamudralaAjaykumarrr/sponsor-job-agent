# Resume Quality Diagnostics

`app/resume_optimizer/quality.py::compute_quality(...) -> QualityReport`

## No fake universal ATS score

This module never emits a single "match %" claimed to be an ATS score, a guaranteed interview
probability, or a hire probability. Every UI/API surface that shows `internal_alignment_score`
pairs it with the literal disclaimer string: *"Internal alignment score -- NOT an ATS score, NOT
an interview/hire probability."*

## `resume_quality.json` shape (`QualityReport.as_dict()`)

```json
{
  "job_id": 1,
  "jd_fingerprint": "...",
  "resume_artifact_hash": "...",
  "required_skill_coverage": {"total": 8, "directly_verified": 5, "transferable": 2, "partial": 0, "missing_count": 1, "missing": ["kafka"]},
  "preferred_skill_coverage": {"total": 3, "directly_verified": 0, "transferable": 2, "partial": 0, "missing_count": 1, "missing": ["kafka"]},
  "responsibility_alignment": {"label": "STRONG", "matched": 2, "total": 3, "detail": "..."},
  "domain_alignment": {"label": "NOT_SPECIFIED", "jd_domains": [], "matched_domains": [], "detail": "..."},
  "title_alignment": {"label": "EXACT", "detail": "..."},
  "keyword_coverage": {"total_keywords": 12, "supported": 9, "ratio": 0.75},
  "experience_evidence_coverage": {"resume_bullets": 4, "unsupported_bullets": 0, "ratio": 1.0},
  "ats_parseability": {"overall": "PASS", "docx": {...}, "pdf": {...}, "txt": {...}, "parser_version": "..."},
  "missing_required": ["kafka"],
  "missing_preferred": ["kafka"],
  "unsupported_jd_items": ["AWS Certified Developer"],
  "selected_evidence": ["skill:python", "responsibility:rest apis", "years_of_experience", ...],
  "claim_check": {"passed": true, "violations": []},
  "warnings": [],
  "alignment_label": "STRONG",
  "internal_alignment_note": "Internal alignment score -- NOT an ATS score, NOT an interview/hire probability.",
  "internal_alignment_score": 72.9,
  "generated_at": "...",
  "optimizer_version": "resume-optimizer-v1",
  "quality_version": "resume-quality-v1"
}
```

## Coverage counting (sections 10-11)

`required_skill_coverage`/`preferred_skill_coverage` only ever count requirements whose
`category` is skill-shaped (`SKILL_CATEGORIES`) -- responsibilities, education, certification,
and years get their own separate diagnostics rather than being folded into one opaque number.
Counts show `directly_verified` and `transferable` separately (never merged into a single
misleading "matched" total), plus a named `missing` list -- gaps are never hidden.

## `alignment_label` and `internal_alignment_score` (section 41, 46)

```
req_ratio  = (required.directly_verified + 0.5 * required.transferable) / required.total   (1.0 if no required items)
pref_ratio = (preferred.directly_verified + 0.5 * preferred.transferable) / preferred.total (1.0 if no preferred items)
resp_ratio = responsibility.matched / responsibility.total                                   (1.0 if none)

internal_alignment_score = round(100 * (0.55 * req_ratio + 0.25 * resp_ratio + 0.20 * pref_ratio), 1)

alignment_label:
  req_ratio < 0.40         -> LOW_ALIGNMENT   (section 40: never "optimized around" via fabrication)
  0.40 <= req_ratio < 0.75 -> MODERATE
  req_ratio >= 0.75        -> STRONG
```

These weights are the ONLY formula used anywhere in the product -- documented here, not hidden
in code. This is a ranking/diagnostic heuristic, not a claim about actual ATS behavior (real
ATS platforms do not share one scoring formula, CLAUDE.md section 2).

## `title_alignment` / `domain_alignment`

- `title_alignment`: `EXACT` (a verified prior title matches the target title exactly),
  `RELATED` (word-token overlap), or `DIFFERENT` -- seniority/title is never inflated to make
  this look better.
- `domain_alignment`: `NOT_SPECIFIED` (JD signaled no domain), `MATCH` (a JD domain signal
  appears in the candidate's verified evidence), or `NO_EVIDENCE`. A domain mismatch is
  explicitly documented as "not a blocker" (section 13) -- shown for transparency only.

## `LOW_ALIGNMENT` behavior (section 40)

When `alignment_label == LOW_ALIGNMENT`, the optimizer does **not** try to close the gap by
inventing content. `missing_required`/`unsupported_jd_items` are populated normally and the
resume is still generated (still `READY` if claim-check and ATS-parse both pass) -- it's simply
an honest, low-alignment resume. See
`tests/test_resume_optimizer_generation.py::test_low_fit_never_fabricates_missing_skills`.

## Persistence and staleness (sections 33-39, 58-59)

One `resume_quality_reports` row per `resume_variants` row (1:1, keyed by `variant_id`). Summary
columns (`alignment_label`, `internal_alignment_score`, coverage counts, `ats_parseability`) are
indexed for dashboard queries without JSON-parsing every row; the full itemized report lives in
`report_json`. A JD or candidate-profile change invalidates the CURRENT variant (marked `STALE`,
CLAUDE.md sections 36/59) -- the old quality report is retained (never deleted) for audit history,
and a new `optimize_resume()` call creates a fresh variant + report.
