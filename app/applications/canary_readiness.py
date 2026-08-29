"""Verified-real-submission READINESS report (Tsenta Remaining-Gaps Closure
V2, section 5).

This module answers a narrower question than "can we submit": "is the
infrastructure for provider X objectively ready for a future, explicitly
authorized real-employer canary run?" It never submits anything, never opens
a browser, and never changes `submission_supported` -- that flag stays
False for every real provider (including Greenhouse) until a genuine
authorized canary actually proves the contract, per this project's own
capability-honesty rule (CLAUDE.md's Greenhouse Verified Submission Contract
V1 section).

Greenhouse gets its OWN dedicated report (`greenhouse_readiness()`, unchanged
by this phase) because it is the only provider with BOTH a dedicated,
structured, published-API form/identity adapter
(`app.applications.providers_greenhouse`) AND a genuine, already-built
provider-specific pre-submit contract
(`app.applications.greenhouse_submit_contract`) that proves 6 of its 8 steps
without a browser at all.

Canary Candidate Pool Expansion + Multi-Provider Readiness V1 adds
`provider_readiness()`, a generic version for any OTHER registered provider,
built on the new `app.applications.provider_submit_contract` (the honest,
provider-neutral generalization of the same 8-step contract). It is
STILL never a second, parallel readiness *system* -- both functions apply
the identical classification rule ("every step that CAN be checked without a
browser, for THIS provider, PASSED; every step that genuinely can't be
checked without a browser is NOT_YET_CHECKED, not a blocker"). The set of
"browser-time" steps is simply wider for a provider with no published
question-schema API (Lever/Ashby/Workable today) than it is for Greenhouse,
because that is the honest, evidence-based truth about what each provider's
real public interface can prove -- never narrowed or widened to make a
provider look more or less ready than it is."""

from dataclasses import dataclass, field as dataclass_field
from typing import Optional

from app.applications.greenhouse_submit_contract import (
    GreenhouseSubmitContract,
    StepStatus,
    build_submit_contract,
)
from app.applications.provider_submit_contract import (
    ProviderSubmitContract,
    build_submit_contract as build_generic_submit_contract,
)

# The steps that can only ever be proven once a real page is open
# (app.applications.greenhouse_submit_contract's own documented limitation).
# Their status is expected to be NOT_YET_CHECKED for a readiness assessment
# that never opens a browser -- that is not a blocker, it is the honest,
# correct state of "not yet checkable this way".
_BROWSER_TIME_STEPS = frozenset({7, 8})

# Providers with NO published application-question-schema API (confirmed by
# reading their own adapter code: `discover_form()` always returns None) can
# only ever prove steps 1, 4, 5 without a browser -- 2 and 3 (and therefore
# 6, which depends on 3's form) also genuinely require one for these
# providers, unlike Greenhouse. Never widen this set to make a provider look
# more ready than its real interface supports; never narrow it to make one
# look less ready.
_GENERIC_BROWSER_TIME_STEPS: dict[str, frozenset] = {
    "lever": frozenset({3, 6, 7, 8}),
    "ashby": frozenset({3, 6, 7, 8}),
    "workable": frozenset({3, 6, 7, 8}),
}


class ReadinessLevel:
    INFRASTRUCTURE_READY = "INFRASTRUCTURE_READY"
    NOT_READY = "NOT_READY"
    NO_ACTIVE_EXECUTION = "NO_ACTIVE_EXECUTION"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"


@dataclass(frozen=True)
class CanaryReadinessReport:
    job_id: int
    provider: str
    level: str
    blocking_reasons: list[str] = dataclass_field(default_factory=list)
    contract: Optional[GreenhouseSubmitContract] = None
    submission_supported: bool = False  # always False here -- see module docstring
    explanation: str = ""

    def as_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "provider": self.provider,
            "level": self.level,
            "blocking_reasons": list(self.blocking_reasons),
            "contract": self.contract.as_dict() if self.contract else None,
            "submission_supported": self.submission_supported,
            "explanation": self.explanation,
        }


def greenhouse_readiness(job_id: int) -> CanaryReadinessReport:
    """Builds the readiness report for one job's active Greenhouse
    execution. Reuses `greenhouse_submit_contract.build_submit_contract()`
    unmodified -- this function only classifies its result, it never
    recomputes any of the underlying facts."""
    contract = build_submit_contract(job_id)
    if contract is None:
        return CanaryReadinessReport(
            job_id=job_id, provider="greenhouse", level=ReadinessLevel.JOB_NOT_FOUND,
            explanation="no job with this id exists",
        )
    if not contract.execution_id:
        return CanaryReadinessReport(
            job_id=job_id, provider="greenhouse", level=ReadinessLevel.NO_ACTIVE_EXECUTION,
            blocking_reasons=list(contract.blocking_reasons), contract=contract,
            explanation="no active application_executions row exists for this job yet",
        )

    non_browser_steps = [s for s in contract.steps if s.number not in _BROWSER_TIME_STEPS]
    non_browser_failed = [s for s in non_browser_steps if s.status == StepStatus.FAILED]
    browser_steps_correctly_pending = all(
        s.status == StepStatus.NOT_YET_CHECKED for s in contract.steps if s.number in _BROWSER_TIME_STEPS
    )

    if non_browser_failed:
        return CanaryReadinessReport(
            job_id=job_id, provider="greenhouse", level=ReadinessLevel.NOT_READY,
            blocking_reasons=[s.detail for s in non_browser_failed], contract=contract,
            explanation=(
                f"{len(non_browser_failed)} browser-independent contract step(s) failed -- infrastructure is "
                "not yet ready for a future canary"
            ),
        )

    return CanaryReadinessReport(
        job_id=job_id, provider="greenhouse", level=ReadinessLevel.INFRASTRUCTURE_READY,
        contract=contract,
        explanation=(
            "every browser-independent step of the Greenhouse submit contract (canonical identity, live "
            "posting status, form fingerprint, approved answer set, approved documents, required fields) "
            "PASSED"
            + ("" if browser_steps_correctly_pending else
               " -- note: browser-time steps report a status other than NOT_YET_CHECKED, which is unexpected "
               "for a readiness check that never opened a browser")
            + ". The two browser-time steps (submit control uniqueness, submit-once claim) remain "
            "NOT_YET_CHECKED, as expected without an open page -- this is infrastructure readiness, not "
            "submission authorization. submission_supported remains False; only a genuine, explicitly-"
            "authorized real-employer canary run could ever justify changing that."
        ),
    )


_SUPPORTED_GENERIC_PROVIDERS = frozenset(_GENERIC_BROWSER_TIME_STEPS.keys())


def provider_readiness(provider_name: str, job_id: int) -> CanaryReadinessReport:
    """The generic version of `greenhouse_readiness()` for any provider in
    `_GENERIC_BROWSER_TIME_STEPS` (Lever/Ashby/Workable as of this phase).
    Same classification rule, same never-a-second-system reuse of the one
    underlying contract builder (`provider_submit_contract.build_submit_contract`)."""
    provider_key = (provider_name or "").lower()
    browser_time_steps = _GENERIC_BROWSER_TIME_STEPS.get(provider_key)
    if browser_time_steps is None:
        return CanaryReadinessReport(
            job_id=job_id, provider=provider_key, level=ReadinessLevel.NOT_READY,
            explanation=f"'{provider_key}' is not yet a supported provider for generic canary-readiness "
                        "reporting (see app.applications.canary_readiness._GENERIC_BROWSER_TIME_STEPS)",
        )

    contract = build_generic_submit_contract(provider_key, job_id)
    if contract is None:
        return CanaryReadinessReport(
            job_id=job_id, provider=provider_key, level=ReadinessLevel.JOB_NOT_FOUND,
            explanation="no job with this id exists, or its provider does not match",
        )
    if not contract.execution_id:
        return CanaryReadinessReport(
            job_id=job_id, provider=provider_key, level=ReadinessLevel.NO_ACTIVE_EXECUTION,
            blocking_reasons=list(contract.blocking_reasons), contract=contract,
            explanation="no active application_executions row exists for this job yet",
        )

    non_browser_steps = [s for s in contract.steps if s.number not in browser_time_steps]
    non_browser_failed = [s for s in non_browser_steps if s.status == StepStatus.FAILED]

    if non_browser_failed:
        return CanaryReadinessReport(
            job_id=job_id, provider=provider_key, level=ReadinessLevel.NOT_READY,
            blocking_reasons=[s.detail for s in non_browser_failed], contract=contract,
            explanation=(
                f"{len(non_browser_failed)} browser-independent contract step(s) failed for {provider_key} -- "
                "infrastructure is not yet ready for a future canary"
            ),
        )

    checkable_names = ", ".join(s.name for s in non_browser_steps)
    return CanaryReadinessReport(
        job_id=job_id, provider=provider_key, level=ReadinessLevel.INFRASTRUCTURE_READY,
        contract=contract,
        explanation=(
            f"every step of {provider_key}'s submit contract that is genuinely checkable without a browser "
            f"({checkable_names}) PASSED. {provider_key} publishes no application-question-schema API, so "
            "form fingerprint and required-fields-complete remain NOT_YET_CHECKED here (honestly bounded "
            "readiness, not Greenhouse's fuller pre-browser picture). submission_supported remains False; "
            "only a genuine, explicitly-authorized real-employer canary run could ever justify changing that."
        ),
    )


def best_canary_candidate() -> dict:
    """A static, evidence-based statement of which provider is the best
    candidate for a future verified-submission canary, and why -- NOT a
    recommendation to run one. Greenhouse is the only provider with a
    dedicated, published-API form/identity adapter AND a genuine
    provider-specific pre-submit contract; every other real provider has, at
    best, browser-DOM-verified fill with no structured API and no dedicated
    submit contract (see app.applications.execution_contract)."""
    return {
        "provider": "greenhouse",
        "reason": (
            "Greenhouse is the only real provider with execution_tier=STRUCTURED_API, "
            "identity_supported sourced from a genuine provider-API canonical-identity function, and "
            "presubmit_validation_supported=True via a dedicated, already-tested provider-specific submit "
            "contract (app.applications.greenhouse_submit_contract) that proves 6 of 8 required facts without "
            "even opening a browser. Lever/Ashby/Workable reach only browser-DOM-verified fill with no "
            "published question schema; SmartRecruiters is CAPTCHA-blocked on its current posting shape; "
            "Workday's login/apply behavior is observed VARIABLE per-tenant. None of those has, or could "
            "safely gain, a dedicated pre-submit contract of Greenhouse's depth without first solving an "
            "external platform limitation this project does not attempt to bypass."
        ),
    }
