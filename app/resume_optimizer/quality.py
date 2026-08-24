"""Resume quality diagnostics (CLAUDE.md Phase 14 sections 2-3, 10-14,
33-34, 40, 46, 89). Deliberately itemized/transparent -- never a single fake
universal "98% ATS match" number. `internal_alignment_score` is explicitly
labeled internal, never called an ATS score or interview probability."""

from dataclasses import dataclass, field

from app.resume.generator import ResumeContent
from app.resume_optimizer.ats_parse import ATSParseReport
from app.resume_optimizer.models import (
    AlignmentLabel,
    ATSParseStatus,
    JDAnalysisResult,
    MatchStatus,
    RequirementMatch,
    RequirementPriority,
    SKILL_CATEGORIES,
)

QUALITY_VERSION = "resume-quality-v1"


@dataclass
class CoverageBucket:
    total: int = 0
    matched: int = 0
    transferable: int = 0
    partial: int = 0
    missing: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "total": self.total, "directly_verified": self.matched, "transferable": self.transferable,
            "partial": self.partial, "missing_count": len(self.missing), "missing": self.missing,
        }


@dataclass
class QualityReport:
    job_id: int
    jd_fingerprint: str
    resume_artifact_hash: str
    required_skill_coverage: CoverageBucket
    preferred_skill_coverage: CoverageBucket
    responsibility_alignment: dict
    domain_alignment: dict
    compensation_alignment: dict
    title_alignment: dict
    keyword_coverage: dict
    experience_evidence_coverage: dict
    ats_parseability: ATSParseReport
    missing_required: list[str]
    missing_preferred: list[str]
    unsupported_jd_items: list[str]
    selected_evidence: list[str]
    claim_check: dict
    warnings: list[str]
    alignment_label: AlignmentLabel
    internal_alignment_score: float
    generated_at: str
    optimizer_version: str
    quality_version: str = QUALITY_VERSION

    def as_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "jd_fingerprint": self.jd_fingerprint,
            "resume_artifact_hash": self.resume_artifact_hash,
            "required_skill_coverage": self.required_skill_coverage.as_dict(),
            "preferred_skill_coverage": self.preferred_skill_coverage.as_dict(),
            "responsibility_alignment": self.responsibility_alignment,
            "domain_alignment": self.domain_alignment,
            "compensation_alignment": self.compensation_alignment,
            "title_alignment": self.title_alignment,
            "keyword_coverage": self.keyword_coverage,
            "experience_evidence_coverage": self.experience_evidence_coverage,
            "ats_parseability": self.ats_parseability.as_dict(),
            "missing_required": self.missing_required,
            "missing_preferred": self.missing_preferred,
            "unsupported_jd_items": self.unsupported_jd_items,
            "selected_evidence": self.selected_evidence,
            "claim_check": self.claim_check,
            "warnings": self.warnings,
            "alignment_label": self.alignment_label.value,
            "internal_alignment_note": "Internal alignment score -- NOT an ATS score, NOT an interview/hire probability.",
            "internal_alignment_score": self.internal_alignment_score,
            "generated_at": self.generated_at,
            "optimizer_version": self.optimizer_version,
            "quality_version": self.quality_version,
        }


def _bucket(matches: list[RequirementMatch], priority: RequirementPriority) -> CoverageBucket:
    bucket = CoverageBucket()
    for m in matches:
        if m.requirement.category not in SKILL_CATEGORIES or m.requirement.priority != priority:
            continue
        bucket.total += 1
        if m.status == MatchStatus.MATCHED:
            bucket.matched += 1
        elif m.status == MatchStatus.TRANSFERABLE:
            bucket.transferable += 1
        elif m.status == MatchStatus.PARTIAL:
            bucket.partial += 1
        else:
            bucket.missing.append(m.requirement.text)
    return bucket


def _title_alignment(job_title: str, resume: ResumeContent) -> dict:
    jt = (job_title or "").lower()
    candidate_titles = [e.title.lower() for e in resume.experience]
    if any(jt == t for t in candidate_titles):
        return {"label": "EXACT", "detail": "Target title matches a verified prior title exactly."}
    jt_tokens = set(jt.split())
    for t in candidate_titles:
        if jt_tokens & set(t.split()):
            return {"label": "RELATED", "detail": f"Target title overlaps verified title '{t}'."}
    return {"label": "DIFFERENT", "detail": "No verified prior title closely matches the target title (seniority/title never inflated)."}


def _responsibility_alignment(matches: list[RequirementMatch]) -> dict:
    resp = [m for m in matches if m.requirement.category.value == "RESPONSIBILITY"]
    if not resp:
        return {"label": "N/A", "matched": 0, "total": 0, "detail": "JD listed no distinct responsibility signals."}
    matched = sum(1 for m in resp if m.status == MatchStatus.MATCHED)
    ratio = matched / len(resp)
    label = "STRONG" if ratio >= 0.7 else ("MODERATE" if ratio >= 0.4 else "WEAK")
    return {"label": label, "matched": matched, "total": len(resp), "detail": f"{matched}/{len(resp)} JD responsibility signals have verified bullet evidence."}


def _domain_alignment(jd_domains: list[str], candidate_domains: list[str]) -> dict:
    if not jd_domains:
        return {"label": "NOT_SPECIFIED", "jd_domains": [], "matched_domains": [], "detail": "JD did not signal a specific industry domain."}
    overlap = sorted(set(jd_domains) & set(candidate_domains))
    return {
        "label": "MATCH" if overlap else "NO_EVIDENCE",
        "jd_domains": jd_domains, "matched_domains": overlap,
        "detail": "A domain mismatch is not a blocker -- shown for transparency only.",
    }


def _compensation_alignment(jd_analysis: JDAnalysisResult, candidate_salary_min_usd: int | None) -> dict:
    """Compensation parsing "where appropriate" (JD intelligence v3): purely
    informational, never a matching/eligibility signal and never a blocker
    -- this JD's parsed figure is only ever compared against the
    candidate's own stated preference (app.candidate.schema.Preferences
    .salary_min_usd), never fabricated when either side is unknown."""
    jd_min, jd_max = jd_analysis.compensation_min, jd_analysis.compensation_max
    if jd_min is None and jd_max is None:
        return {
            "label": "NOT_SPECIFIED", "jd_compensation_min": None, "jd_compensation_max": None,
            "jd_compensation_period": jd_analysis.compensation_period,
            "detail": "JD did not state a compensation figure this parser could confidently extract.",
        }
    if candidate_salary_min_usd is None:
        return {
            "label": "CANDIDATE_PREFERENCE_UNKNOWN", "jd_compensation_min": jd_min, "jd_compensation_max": jd_max,
            "jd_compensation_period": jd_analysis.compensation_period,
            "detail": "Candidate salary_min_usd preference is not set -- comparison skipped, not guessed.",
        }
    jd_ceiling = jd_max if jd_max is not None else jd_min
    label = "MEETS_OR_ABOVE_PREFERENCE" if jd_ceiling >= candidate_salary_min_usd else "BELOW_PREFERENCE"
    return {
        "label": label, "jd_compensation_min": jd_min, "jd_compensation_max": jd_max,
        "jd_compensation_period": jd_analysis.compensation_period,
        "candidate_salary_min_usd": candidate_salary_min_usd,
        "detail": "Informational only -- never a matching/eligibility blocker.",
    }


def compute_quality(
    *,
    job_id: int,
    jd_fingerprint: str,
    resume_artifact_hash: str,
    job_title: str,
    jd_analysis: JDAnalysisResult,
    matches: list[RequirementMatch],
    resume: ResumeContent,
    ats_report: ATSParseReport,
    claim_violations: list[str],
    optimizer_version: str,
    generated_at: str,
    candidate_domains: list[str] | None = None,
    candidate_salary_min_usd: int | None = None,
) -> QualityReport:
    required_bucket = _bucket(matches, RequirementPriority.REQUIRED)
    preferred_bucket = _bucket(matches, RequirementPriority.PREFERRED)

    all_skill_matches = [m for m in matches if m.requirement.category in SKILL_CATEGORIES]
    supported = sum(1 for m in all_skill_matches if m.status in (MatchStatus.MATCHED, MatchStatus.TRANSFERABLE))
    keyword_coverage = {
        "total_keywords": len(all_skill_matches),
        "supported": supported,
        "ratio": round(supported / len(all_skill_matches), 3) if all_skill_matches else 0.0,
    }

    selected_evidence = sorted({eid for m in matches for eid in m.evidence_ids})
    experience_bullet_count = sum(len(e.bullets) for e in resume.experience) + sum(len(p.bullets) for p in resume.projects)
    experience_evidence_coverage = {
        "resume_bullets": experience_bullet_count,
        "unsupported_bullets": len(claim_violations),
        "ratio": 1.0 if experience_bullet_count and not claim_violations else (0.0 if not experience_bullet_count else round(1 - len(claim_violations) / experience_bullet_count, 3)),
    }

    missing_required = [m.requirement.text for m in matches if m.requirement.priority == RequirementPriority.REQUIRED and m.status == MatchStatus.MISSING]
    missing_preferred = [m.requirement.text for m in matches if m.requirement.priority == RequirementPriority.PREFERRED and m.status == MatchStatus.MISSING]
    # CLAUDE.md section 2/61: UNSUPPORTED_JD_ITEMS -- JD items that can never
    # be satisfied by any verified evidence and are certifications/education,
    # shown honestly rather than hidden.
    unsupported = [
        m.requirement.text for m in matches
        if m.status == MatchStatus.MISSING and m.requirement.category.value in ("CERTIFICATION", "EDUCATION")
    ]

    warnings = list(claim_violations)
    if ats_report.overall != ATSParseStatus.PASS:
        warnings.append(f"ATS parseability {ats_report.overall.value}: {'; '.join(ats_report.docx.reasons + ats_report.pdf.reasons + ats_report.txt.reasons)}")

    # CLAUDE.md section 41: transparent, documented weighted combination --
    # never presented as a probability of interview/hire (section 2/89).
    req_ratio = (required_bucket.matched + 0.5 * required_bucket.transferable) / required_bucket.total if required_bucket.total else 1.0
    pref_ratio = (preferred_bucket.matched + 0.5 * preferred_bucket.transferable) / preferred_bucket.total if preferred_bucket.total else 1.0
    resp = _responsibility_alignment(matches)
    resp_ratio = (resp["matched"] / resp["total"]) if resp["total"] else 1.0
    internal_alignment_score = round(100 * (0.55 * req_ratio + 0.25 * resp_ratio + 0.20 * pref_ratio), 1)

    if required_bucket.total and req_ratio < 0.4:
        alignment_label = AlignmentLabel.LOW_ALIGNMENT
    elif required_bucket.total and req_ratio < 0.75:
        alignment_label = AlignmentLabel.MODERATE
    else:
        alignment_label = AlignmentLabel.STRONG

    return QualityReport(
        job_id=job_id, jd_fingerprint=jd_fingerprint, resume_artifact_hash=resume_artifact_hash,
        required_skill_coverage=required_bucket, preferred_skill_coverage=preferred_bucket,
        responsibility_alignment=resp,
        domain_alignment=_domain_alignment(jd_analysis.domain_signals, candidate_domains or []),
        compensation_alignment=_compensation_alignment(jd_analysis, candidate_salary_min_usd),
        title_alignment=_title_alignment(job_title, resume),
        keyword_coverage=keyword_coverage,
        experience_evidence_coverage=experience_evidence_coverage,
        ats_parseability=ats_report,
        missing_required=missing_required, missing_preferred=missing_preferred,
        unsupported_jd_items=unsupported,
        selected_evidence=selected_evidence,
        claim_check={"passed": not claim_violations, "violations": claim_violations},
        warnings=warnings,
        alignment_label=alignment_label,
        internal_alignment_score=internal_alignment_score,
        generated_at=generated_at,
        optimizer_version=optimizer_version,
    )
