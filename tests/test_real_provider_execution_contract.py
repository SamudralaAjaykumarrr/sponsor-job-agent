"""Real Provider Execution V1: the provider EXECUTION contract's truthfulness.

The brief's PROVIDER CONTRACT and CAPABILITY AUDIT requirements, plus the
single rule everything else in this feature hangs off: browser fill
capability is NOT submission capability.
"""

import pytest

from app.applications.browser_capability_matrix import (
    BrowserVerification,
    ConfirmationCaptureLevel,
    all_rows as browser_rows,
)
from app.applications.execution_contract import (
    AUDIT_PROVIDERS,
    CapabilitySource,
    all_contracts,
    audit_contracts,
    build_contract,
    build_matrix,
    render_audit,
)
from app.applications.provider_registry import all_application_capabilities

_SEVEN_FLAGS = (
    "discovery_supported", "form_discovery_supported", "fill_supported", "upload_supported",
    "assist_supported", "submission_supported", "confirmation_supported",
)


def test_contract_exposes_all_seven_flags_separately():
    """"Do NOT collapse these into one boolean." Every flag is present and
    independently addressable on every provider's contract."""
    for contract in all_contracts():
        d = contract.as_dict()
        for flag in _SEVEN_FLAGS:
            assert flag in d, f"{contract.provider} is missing {flag}"
            assert isinstance(d[flag], bool)


def test_only_mock_ats_ever_reports_submission_supported():
    submitters = [c.provider for c in all_contracts() if c.submission_supported]
    assert submitters == ["mock_ats"]


@pytest.mark.parametrize("provider", ["greenhouse", "lever"])
def test_browser_fill_capability_is_not_submission_capability(provider):
    """The heart of the brief: Greenhouse and Lever both have genuinely
    working fill/upload/assist capability, and both still report
    submission_supported=False."""
    contract = build_contract(provider)
    assert contract.fill_supported is True
    assert contract.upload_supported is True
    assert contract.assist_supported is True
    assert contract.submission_supported is False
    assert contract.submission_source == CapabilitySource.NONE
    assert "submission_supported is False" in contract.submission_evidence


def test_submission_flag_is_read_from_application_capabilities_alone():
    """A contract's submission flag must equal its ApplicationCapabilities
    row exactly -- never OR-ed with a browser observation."""
    caps = {c["provider"]: c for c in all_application_capabilities() if c["provider"] != "generic"}
    for contract in all_contracts():
        expected = bool(caps.get(contract.provider, {}).get("submission_supported"))
        assert contract.submission_supported is expected, contract.provider


def test_lever_form_discovery_is_browser_sourced_not_api_sourced():
    """Lever's public API genuinely exposes no question schema, so the
    adapter's own ApplicationCapabilities keeps form_discovery_supported
    False -- while the unified contract honestly reports the capability as
    reachable through the live-verified browser engine, naming that source."""
    from app.applications.providers_lever import LeverApplicationProvider

    assert LeverApplicationProvider.get_capabilities().form_discovery_supported is False
    contract = build_contract("lever")
    assert contract.form_discovery_supported is True
    assert contract.form_discovery_source == CapabilitySource.BROWSER_LIVE_VERIFIED


def test_greenhouse_form_discovery_is_api_sourced():
    contract = build_contract("greenhouse")
    assert contract.form_discovery_supported is True
    assert contract.form_discovery_source == CapabilitySource.PROVIDER_API


def test_untested_providers_report_no_assist_capability():
    """A provider whose real form has never been opened must not gain any
    capability from the generic engine merely existing."""
    for provider in ("workday", "smartrecruiters"):
        contract = build_contract(provider)
        assert contract.assist_supported is False
        assert contract.form_discovery_supported is False
        assert contract.confirmation_supported is False


def test_confirmation_supported_matches_recorded_capture_evidence():
    rows = {r["provider"]: r for r in browser_rows()}
    for contract in all_contracts():
        row = rows.get(contract.provider)
        if row is None:
            continue
        observed = row["confirmation_capture_level"] != ConfirmationCaptureLevel.NOT_OBSERVED.value
        caps = {c["provider"]: c for c in all_application_capabilities()}.get(contract.provider, {})
        expected = observed or bool(caps.get("confirmation_detection_supported"))
        assert contract.confirmation_supported is expected, contract.provider


def test_no_browser_row_ever_claims_final_submit_automation():
    for row in browser_rows():
        assert row["final_submit_automation"] is False, row["provider"]


def test_confirmation_capture_level_never_claims_a_real_submission():
    """LIVE_SUBMISSION_VERIFIED could only be earned by genuinely submitting
    an application to a real employer, which this project never does -- no
    row may carry it."""
    for row in browser_rows():
        assert row["confirmation_capture_level"] != ConfirmationCaptureLevel.LIVE_SUBMISSION_VERIFIED.value


def test_capability_source_is_never_inflated_above_its_evidence():
    """A provider whose browser evidence is FIXTURE_ONLY must never report a
    LIVE-verified source for any flag."""
    rows = {r["provider"]: r for r in browser_rows()}
    for contract in all_contracts():
        row = rows.get(contract.provider)
        if row is None or row["verification"] == BrowserVerification.LIVE_FORM_VERIFIED.value:
            continue
        for source in (contract.form_discovery_source, contract.fill_source, contract.upload_source,
                        contract.assist_source):
            assert source != CapabilitySource.BROWSER_LIVE_VERIFIED, contract.provider


def test_audit_covers_exactly_the_briefs_named_providers():
    assert [c.provider for c in audit_contracts()] == list(AUDIT_PROVIDERS)
    for name in ("mock_ats", "greenhouse", "lever", "ashby", "workday", "smartrecruiters", "workable"):
        assert name in AUDIT_PROVIDERS


def test_render_audit_states_submission_and_confirmation_for_every_provider():
    text = render_audit()
    for provider in AUDIT_PROVIDERS:
        assert f"Provider: {provider}" in text
    assert text.count("submission_supported") >= len(AUDIT_PROVIDERS)
    assert text.count("confirmation_supported") >= len(AUDIT_PROVIDERS)
    assert "browser fill/assist capability is NEVER submission capability" in text


def test_build_matrix_rows_are_serializable_dicts():
    matrix = build_matrix()
    assert {c[0] for c in matrix["columns"]} >= set(_SEVEN_FLAGS)
    assert all(isinstance(row, dict) for row in matrix["rows"])


def test_providers_without_a_dedicated_adapter_report_the_generic_fallback_policy():
    """Ashby/Workday/... have no dedicated ApplicationProvider, so the
    product genuinely falls back to GenericAssistOnlyProvider -- the audit
    must report that ASSIST_ONLY reality rather than a bare UNSUPPORTED."""
    contract = build_contract("ashby")
    assert contract.has_application_adapter is False
    assert contract.automation_policy == "ASSIST_ONLY"


def test_doctor_contract_checks_pass_on_the_real_registries(tmp_env):
    from app.applications.doctor import run_doctor

    report = run_doctor()
    drift = [i for i in report.issues if i.check.startswith("execution_contract")]
    assert drift == [], drift


# --- dashboard / API surface --------------------------------------------------

def test_execution_contract_page_renders_and_separates_submission(tmp_env):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        response = client.get("/applications/execution-contract")
    assert response.status_code == 200
    body = response.text
    assert "Provider Execution Contract" in body
    # The safety callout must be present and unambiguous.
    assert "Browser fill capability is not submission capability" in body
    for provider in AUDIT_PROVIDERS:
        assert provider in body


def test_execution_contract_api_returns_all_seven_flags(tmp_env):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        response = client.get("/api/applications/execution-contract")
    assert response.status_code == 200
    providers = {row["provider"]: row for row in response.json()["providers"]}
    for flag in _SEVEN_FLAGS:
        assert flag in providers["greenhouse"]
    assert providers["greenhouse"]["submission_supported"] is False
    assert providers["lever"]["submission_supported"] is False
    assert providers["mock_ats"]["submission_supported"] is True
