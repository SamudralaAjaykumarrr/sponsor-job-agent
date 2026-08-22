"""Deterministic acquisition-priority scoring (CLAUDE.md Phase 6 section 26).

Answers "which registry_companies rows are worth verifying/polling first",
NOT "which jobs are most likely to result in an interview" -- interview
probability is explicitly excluded from every input here.

CRITICAL, durable rule: `has_sponsorship_history_signal` may raise a
company's acquisition priority (worth discovering/verifying/polling sooner),
but it NEVER touches a specific job's sponsorship_status. That gate is
owned entirely by app.sponsorship.classifier, which reads only the JD text
and the separate known-sponsors reference list -- this module, and the
app.sponsorship.evidence table it may read from, are never consulted there."""

from dataclasses import dataclass, field

from app.providers.capabilities import SupportLevel

# Weights are deliberately simple, explicit, and auditable -- not a learned
# model. Each is a fixed point contribution; the total is not bounded to a
# fixed max, so `priority_score` is a relative ranking signal, not a
# normalized 0-1 probability of anything.
_WEIGHT_US_EMPLOYER = 10
_WEIGHT_TECHNICAL_EMPLOYER = 10
_WEIGHT_KNOWN_TECH_HIRING = 5
_WEIGHT_SUPPORT_LEVEL = {
    SupportLevel.FULL: 15, SupportLevel.PARTIAL: 8, SupportLevel.EXPERIMENTAL: 3, SupportLevel.UNSUPPORTED: 0,
}
_WEIGHT_HISTORICAL_JOB_YIELD_PER_JOB = 0.1
_WEIGHT_HISTORICAL_JOB_YIELD_CAP = 10
_WEIGHT_REGISTRY_CONFIDENCE = 0.1  # confidence is already 0-100
_WEIGHT_SPONSORSHIP_HISTORY_SIGNAL = 8


@dataclass
class PriorityInputs:
    is_us_employer: bool = False
    is_technical_employer: bool = False
    known_technology_hiring: bool = False
    support_level: SupportLevel = SupportLevel.UNSUPPORTED
    historical_job_yield: float = 0.0
    registry_confidence: int = 0
    has_sponsorship_history_signal: bool = False


@dataclass
class PriorityResult:
    score: float
    reasons: list[str] = field(default_factory=list)


def compute_priority(inputs: PriorityInputs) -> PriorityResult:
    score = 0.0
    reasons: list[str] = []

    if inputs.is_us_employer:
        score += _WEIGHT_US_EMPLOYER
        reasons.append("US employer")
    if inputs.is_technical_employer:
        score += _WEIGHT_TECHNICAL_EMPLOYER
        reasons.append("technical/software employer")
    if inputs.known_technology_hiring:
        score += _WEIGHT_KNOWN_TECH_HIRING
        reasons.append("known history of technology hiring")

    support_weight = _WEIGHT_SUPPORT_LEVEL.get(inputs.support_level, 0)
    if support_weight:
        score += support_weight
        reasons.append(f"provider support level {inputs.support_level.value}")

    yield_score = min(inputs.historical_job_yield * _WEIGHT_HISTORICAL_JOB_YIELD_PER_JOB, _WEIGHT_HISTORICAL_JOB_YIELD_CAP)
    if yield_score:
        score += yield_score
        reasons.append(f"historical job yield {inputs.historical_job_yield:.1f}")

    if inputs.registry_confidence:
        score += inputs.registry_confidence * _WEIGHT_REGISTRY_CONFIDENCE
        reasons.append(f"registry confidence {inputs.registry_confidence}")

    if inputs.has_sponsorship_history_signal:
        score += _WEIGHT_SPONSORSHIP_HISTORY_SIGNAL
        reasons.append(
            "company has historical sponsorship-history signal -- affects ACQUISITION PRIORITY ONLY, "
            "never a specific job's sponsorship_status"
        )

    return PriorityResult(score=round(score, 2), reasons=reasons)
