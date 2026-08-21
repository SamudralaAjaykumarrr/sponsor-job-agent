from app.candidate.profile import load_profile, missing_fields
from app.config import NEEDS_USER_INPUT


def test_blank_profile_is_all_needs_user_input(tmp_env):
    profile = load_profile()
    missing = missing_fields(profile)
    assert "contact.full_name" in missing
    assert "work_authorization.current_status" in missing
    assert len(missing) > 5


def test_complete_profile_has_no_missing_contact_fields(sample_profile):
    missing = missing_fields(sample_profile)
    assert not any(m.startswith("contact.") for m in missing)


def test_needs_user_input_sentinel_value():
    assert NEEDS_USER_INPUT == "NEEDS_USER_INPUT"
