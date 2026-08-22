"""CLAUDE.md Phase 11 sections 10, 13, 45: Workday tenant/site parsing and
per-tenant capability tracking. Never touches network/browser."""

import pytest

from app.applications.workday_tenant import (
    get_observation,
    list_observations,
    parse_workday_tenant,
    record_observation,
    render_tenant_matrix,
)


def test_parse_candidate_facing_url():
    info = parse_workday_tenant(
        "https://acme.wd5.myworkdayjobs.com/External/job/Remote/Software-Engineer_R-1234"
    )
    assert info.recognized is True
    assert info.tenant == "acme"
    assert info.site == "External"
    assert info.requisition_id == "R-1234"


def test_parse_cxs_api_url():
    info = parse_workday_tenant("https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs")
    assert info.recognized is True
    assert info.tenant == "acme"
    assert info.site == "External"


def test_parse_unrelated_url_not_recognized():
    info = parse_workday_tenant("https://boards.greenhouse.io/acme/jobs/1")
    assert info.recognized is False
    assert info.tenant == ""


def test_parse_never_guesses_missing_site():
    info = parse_workday_tenant("https://acme.wd5.myworkdayjobs.com/")
    assert info.recognized is True
    assert info.tenant == "acme"
    assert info.site == ""


def test_record_and_get_observation(tmp_env):
    record_observation("acme", "External", "acme.wd5.myworkdayjobs.com",
                        landing_navigation=True, login_required=True, notes="observed live")
    row = get_observation("acme", "External")
    assert row["landing_navigation"] == 1
    assert row["login_required"] == 1
    assert row["resume_upload"] is None  # not yet observed -- never guessed


def test_record_observation_only_updates_passed_keys(tmp_env):
    record_observation("acme", "External", "acme.wd5.myworkdayjobs.com", landing_navigation=True)
    record_observation("acme", "External", "acme.wd5.myworkdayjobs.com", login_required=False)
    row = get_observation("acme", "External")
    assert row["landing_navigation"] == 1
    assert row["login_required"] == 0


def test_unknown_capability_key_rejected(tmp_env):
    with pytest.raises(ValueError):
        record_observation("acme", "External", "acme.wd5.myworkdayjobs.com", not_a_real_capability=True)


def test_different_tenants_never_conflated(tmp_env):
    """CLAUDE.md Phase 11 section 45: per-tenant/site rows, never one
    blanket Workday claim."""
    record_observation("acme", "External", "acme.wd5.myworkdayjobs.com", login_required=True)
    record_observation("globex", "External", "globex.wd3.myworkdaysite.com", login_required=False)
    acme = get_observation("acme", "External")
    globex = get_observation("globex", "External")
    assert acme["login_required"] == 1
    assert globex["login_required"] == 0


def test_render_tenant_matrix_never_collapses_tenants(tmp_env):
    record_observation("acme", "External", "acme.wd5.myworkdayjobs.com", login_required=True)
    record_observation("globex", "External", "globex.wd3.myworkdaysite.com", login_required=False)
    text = render_tenant_matrix()
    assert "Tenant: acme" in text
    assert "Tenant: globex" in text


def test_render_tenant_matrix_empty_is_honest(tmp_env):
    text = render_tenant_matrix()
    assert "No tenant/site has been observed yet" in text


def test_list_observations(tmp_env):
    record_observation("acme", "External", "acme.wd5.myworkdayjobs.com", login_required=True)
    rows = list_observations()
    assert len(rows) == 1
    assert rows[0]["tenant"] == "acme"
