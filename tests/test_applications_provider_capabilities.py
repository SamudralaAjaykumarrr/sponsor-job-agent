"""CLAUDE.md Phase 8 section 56: application provider capability matrix
honesty -- never inflate a capability that isn't backed by working, tested
code."""

from app.applications.provider_registry import all_application_capabilities
from app.applications.providers_generic import GenericAssistOnlyProvider
from app.applications.providers_greenhouse import GreenhouseApplicationProvider
from app.applications.providers_lever import LeverApplicationProvider
from app.applications.mock_ats import MockATSProvider


def test_only_mock_ats_claims_submission_supported():
    matrix = all_application_capabilities()
    submitters = [c["provider"] for c in matrix if c["submission_supported"]]
    assert submitters == ["mock_ats"]


def test_lever_honestly_unsupported_for_form_discovery():
    caps = LeverApplicationProvider.get_capabilities()
    assert caps.form_discovery_supported is False
    assert caps.submission_supported is False
    assert caps.support_level.value == "UNSUPPORTED"
    assert caps.live_validated is True  # the ABSENCE of the interface was itself verified live


def test_generic_provider_never_claims_discovery_or_submission():
    caps = GenericAssistOnlyProvider.get_capabilities()
    assert caps.form_discovery_supported is False
    assert caps.submission_supported is False
    assert caps.automation_policy.value == "ASSIST_ONLY"


def test_generic_row_honestly_names_the_real_providers_it_covers():
    """Provider Post-Approval Execution V1: the single 'generic' capability
    row must name which real, named providers (Ashby/Workday/SmartRecruiters/
    Workable, plus any other discovery-registered provider with no dedicated
    application-layer adapter) it actually stands in for -- purely derived
    from app.providers.registry.all_provider_names() minus the app-layer
    _PROVIDERS keys, never a hand-maintained duplicate list that could drift."""
    matrix = all_application_capabilities()
    generic_row = next(c for c in matrix if c["provider"] == "generic")
    for name in ("ashby", "workday", "smartrecruiters", "workable"):
        assert name in generic_row["covers_provider_names"]
    # mock_ats/greenhouse/lever have dedicated adapters -- never listed here.
    assert "greenhouse" not in generic_row["covers_provider_names"]
    assert "lever" not in generic_row["covers_provider_names"]
    assert "mock_ats" not in generic_row["covers_provider_names"]
    for c in matrix:
        if c["provider"] != "generic":
            assert "covers_provider_names" not in c


def test_greenhouse_claims_discovery_but_not_submission():
    caps = GreenhouseApplicationProvider.get_capabilities()
    assert caps.form_discovery_supported is True
    assert caps.field_mapping_supported is True
    assert caps.submission_supported is False


def test_mock_ats_is_full_support_for_testing_only():
    caps = MockATSProvider.get_capabilities()
    assert caps.support_level.value == "FULL"
    assert caps.submission_supported is True
    assert "never a real ATS" in caps.notes
