"""Tsenta Remaining-Gaps Closure V2: the two new execution-contract axes
(`identity_supported`, `presubmit_validation_supported`) and the derived
`execution_tier` classification.

Same non-negotiable rule as Real Provider Execution V1: fill is not submit,
and neither of these new axes may ever be inflated beyond genuine evidence
(app.applications.doctor's own consistency checks statically re-derive both
-- see test_doctor.py for that coverage; this file tests the module's own
public behavior)."""

from app.applications.execution_contract import (
    CapabilitySource,
    ExecutionTier,
    all_contracts,
    build_contract,
)


def test_every_contract_reports_identity_and_presubmit_fields():
    for contract in all_contracts():
        d = contract.as_dict()
        assert "identity_supported" in d
        assert "presubmit_validation_supported" in d
        assert "execution_tier" in d
        assert isinstance(d["identity_supported"], bool)
        assert isinstance(d["presubmit_validation_supported"], bool)


def test_greenhouse_has_full_structured_execution_tier():
    contract = build_contract("greenhouse")
    assert contract.identity_supported is True
    assert contract.identity_source == CapabilitySource.PROVIDER_API
    assert contract.presubmit_validation_supported is True
    assert contract.execution_tier == ExecutionTier.STRUCTURED_API


def test_browser_only_providers_never_claim_structured_api_tier():
    """Lever/Ashby/Workable have real, browser-verified FILL but no
    published question schema -- their form comes from the DOM, not an API,
    so they must never be reported as STRUCTURED_API (that would conflate
    "a dedicated adapter class exists" with "the form is API-sourced")."""
    for provider in ("lever", "ashby", "workable"):
        contract = build_contract(provider)
        assert contract.execution_tier != ExecutionTier.STRUCTURED_API, provider
        assert contract.execution_tier == ExecutionTier.ASSIST_CAPABLE, provider
        # identity IS supported for these -- it is a generic, browser-reachability
        # property (job_identity runs on every real navigation), not an API fact.
        assert contract.identity_supported is True, provider
        assert contract.identity_source != CapabilitySource.PROVIDER_API, provider
        # no dedicated provider-specific pre-submit contract exists for these yet
        assert contract.presubmit_validation_supported is False, provider


def test_pure_discovery_providers_report_discovery_only_tier():
    for provider in ("bamboohr", "breezy", "comeet", "icims", "jazzhr", "jobvite",
                      "oracle", "pinpoint", "recruitee", "teamtailor"):
        contract = build_contract(provider)
        assert contract.execution_tier == ExecutionTier.DISCOVERY_ONLY, provider
        assert contract.identity_supported is False, provider
        assert contract.presubmit_validation_supported is False, provider


def test_smartrecruiters_and_workday_remain_discovery_only_not_fabricated():
    """Both are real, investigated external limitations (SmartRecruiters:
    CAPTCHA-blocked on its current posting shape; Workday: per-tenant
    VARIABLE login behavior) -- neither has genuine field_discovery=True in
    the browser capability matrix, so neither may be promoted here either."""
    for provider in ("smartrecruiters", "workday"):
        contract = build_contract(provider)
        assert contract.execution_tier == ExecutionTier.DISCOVERY_ONLY, provider


def test_mock_ats_is_verified_submit_tier():
    contract = build_contract("mock_ats")
    assert contract.execution_tier == ExecutionTier.VERIFIED_SUBMIT
    assert contract.submission_supported is True


def test_no_real_provider_ever_reaches_verified_submit_tier():
    for contract in all_contracts():
        if contract.provider == "mock_ats":
            continue
        assert contract.execution_tier != ExecutionTier.VERIFIED_SUBMIT, contract.provider
        assert contract.submission_supported is False, contract.provider


def test_execution_tier_is_a_total_order_every_provider_gets_exactly_one():
    valid = {t.value for t in ExecutionTier}
    for contract in all_contracts():
        assert contract.execution_tier.value in valid
