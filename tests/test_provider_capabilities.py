from app.providers.capabilities import SupportLevel
from app.providers.registry import all_capabilities, all_provider_names, get_capabilities

EXPECTED_FULL = {"greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee", "breezy"}
EXPECTED_PARTIAL = {"bamboohr", "workday"}
EXPECTED_EXPERIMENTAL = {"comeet"}
EXPECTED_UNSUPPORTED = {"teamtailor", "jobvite", "pinpoint", "jazzhr", "icims", "oracle"}


def test_all_provider_names_cover_every_target_provider():
    names = set(all_provider_names())
    assert names == EXPECTED_FULL | EXPECTED_PARTIAL | EXPECTED_EXPERIMENTAL | EXPECTED_UNSUPPORTED


def test_support_levels_match_documented_matrix():
    for name in EXPECTED_FULL:
        assert get_capabilities(name).support_level == SupportLevel.FULL, name
    for name in EXPECTED_PARTIAL:
        assert get_capabilities(name).support_level == SupportLevel.PARTIAL, name
    for name in EXPECTED_EXPERIMENTAL:
        assert get_capabilities(name).support_level == SupportLevel.EXPERIMENTAL, name
    for name in EXPECTED_UNSUPPORTED:
        assert get_capabilities(name).support_level == SupportLevel.UNSUPPORTED, name


def test_unsupported_providers_never_claim_discovery():
    for name in EXPECTED_UNSUPPORTED:
        cap = get_capabilities(name)
        assert cap.discovery_supported is False
        assert cap.public_interface is False or cap.requires_credentials is True


def test_full_providers_claim_discovery_and_public_interface():
    for name in EXPECTED_FULL:
        cap = get_capabilities(name)
        assert cap.discovery_supported is True
        assert cap.public_interface is True
        assert cap.requires_credentials is False


def test_no_provider_claims_submission_supported():
    """CLAUDE.md: never auto-submit applications. No provider connector is
    allowed to claim it can submit on the candidate's behalf."""
    for cap in all_capabilities():
        assert cap.submission_supported is False


def test_capabilities_serialize_to_plain_dict():
    cap = get_capabilities("greenhouse")
    d = cap.as_dict()
    assert d["support_level"] == "FULL"
    assert isinstance(d["notes"], str)


def test_unknown_provider_capabilities_returns_none():
    assert get_capabilities("does-not-exist") is None
