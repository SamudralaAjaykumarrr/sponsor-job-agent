"""Transparent match-priority ranking (CLAUDE.md Phase 14 section 41).
Combines deterministic, already-computed components into one auditable
ranking value -- explicitly NOT an interview/hire probability (same
constraint app.registry.acquisition_priority already established for a
different ranking in Phase 6). Never overrides the sponsorship
(CLAUDE.md section 42) or FULL_TIME (section 43) hard gates -- this module
only ranks among jobs that already passed those gates."""

from app.models import Job, SponsorshipStatus, WorkArrangement

# Documented weights (out of 100) -- CLAUDE.md section 41 "document weights".
WEIGHTS = {
    "required_coverage": 30,
    "responsibility_alignment": 15,
    "preferred_coverage": 10,
    "freshness": 15,
    "work_arrangement": 15,
    "sponsorship": 10,
    "domain_alignment": 5,
}

_ARRANGEMENT_SCORE = {WorkArrangement.REMOTE: 1.0, WorkArrangement.HYBRID: 0.6, WorkArrangement.ONSITE: 0.3, WorkArrangement.UNKNOWN: 0.0}
_SPONSORSHIP_SCORE = {SponsorshipStatus.CONFIRMED_SPONSOR: 1.0, SponsorshipStatus.LIKELY_SPONSOR: 0.5}
_RESPONSIBILITY_LABEL_SCORE = {"STRONG": 1.0, "MODERATE": 0.6, "WEAK": 0.2, "N/A": 0.5}
_FRESHNESS_SCORE = {"MAXIMUM": 1.0, "VERY_HIGH": 0.8, "HIGH": 0.6, "MODERATE": 0.35, "LOWER": 0.1}


def compute_alignment_priority(job: Job, quality_report: dict | None) -> dict:
    """Returns {"score": 0-100, "components": [...]} -- every component's
    raw input and weighted contribution is included so the number is always
    auditable, never a black box."""
    components = []

    if quality_report:
        req_cov = quality_report.get("required_skill_coverage", {})
        req_total = req_cov.get("total", 0)
        req_ratio = (
            (req_cov.get("directly_verified", 0) + 0.5 * req_cov.get("transferable", 0)) / req_total
            if req_total else 1.0
        )
        pref_cov = quality_report.get("preferred_skill_coverage", {})
        pref_total = pref_cov.get("total", 0)
        pref_ratio = (
            (pref_cov.get("directly_verified", 0) + 0.5 * pref_cov.get("transferable", 0)) / pref_total
            if pref_total else 1.0
        )
        resp_label = quality_report.get("responsibility_alignment", {}).get("label", "N/A")
        resp_ratio = _RESPONSIBILITY_LABEL_SCORE.get(resp_label, 0.5)
        domain = quality_report.get("domain_alignment", {})
        domain_ratio = 1.0 if domain.get("label") == "MATCH" else (0.5 if domain.get("label") == "NOT_SPECIFIED" else 0.2)
    else:
        req_ratio = pref_ratio = resp_ratio = domain_ratio = 0.0

    arrangement_ratio = _ARRANGEMENT_SCORE.get(job.work_arrangement, 0.0)
    sponsorship_ratio = _SPONSORSHIP_SCORE.get(job.sponsorship_status, 0.0)
    freshness_ratio = _FRESHNESS_SCORE.get(job.freshness_tier.value if hasattr(job.freshness_tier, "value") else job.freshness_tier, 0.1)

    for name, ratio in [
        ("required_coverage", req_ratio), ("responsibility_alignment", resp_ratio),
        ("preferred_coverage", pref_ratio), ("freshness", freshness_ratio),
        ("work_arrangement", arrangement_ratio), ("sponsorship", sponsorship_ratio),
        ("domain_alignment", domain_ratio),
    ]:
        weight = WEIGHTS[name]
        components.append({"name": name, "weight": weight, "ratio": round(ratio, 3), "contribution": round(weight * ratio, 2)})

    score = round(sum(c["contribution"] for c in components), 1)
    return {"score": score, "components": components, "note": "Internal ranking signal -- not an interview/hire probability."}
