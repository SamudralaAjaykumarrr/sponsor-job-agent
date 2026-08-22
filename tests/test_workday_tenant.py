"""CLAUDE.md Phase 11 sections 10, 13, 45: Workday tenant/site parsing and
per-tenant capability tracking. Never touches network/browser."""

import pytest

from app.applications.workday_tenant import (
    WorkdayStability,
    classify_stability,
    get_observation,
    list_all_attempts,
    list_attempts,
    list_observations,
    parse_workday_tenant,
    record_attempt,
    record_observation,
    render_tenant_matrix,
    stability_report,
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


# --- CLAUDE.md Phase 12 sections 18-21, 54, 77: repeated attempts/stability ---

def test_record_attempt_appends_never_overwrites(tmp_env):
    record_attempt("acme", "External", "acme.wd5.myworkdayjobs.com", result="NAVIGATION_SAFE", fields_detected=0)
    record_attempt("acme", "External", "acme.wd5.myworkdayjobs.com", result="NAVIGATION_SAFE", fields_detected=0)
    attempts = list_attempts("acme", "External")
    assert len(attempts) == 2


def test_stability_unverified_with_zero_or_one_attempt(tmp_env):
    assert classify_stability("acme", "External") == WorkdayStability.UNVERIFIED
    record_attempt("acme", "External", "acme.wd5.myworkdayjobs.com", result="NAVIGATION_SAFE")
    assert classify_stability("acme", "External") == WorkdayStability.UNVERIFIED


def test_stability_stable_when_all_attempts_agree(tmp_env):
    for _ in range(3):
        record_attempt("acme", "External", "acme.wd5.myworkdayjobs.com", result="NAVIGATION_SAFE")
    assert classify_stability("acme", "External") == WorkdayStability.STABLE


def test_stability_variable_when_attempts_disagree(tmp_env):
    """CLAUDE.md Phase 12 sections 20, 77: honest disagreement, never
    cherry-picked to the more favorable run -- mirrors the real Walmart
    Workday tenant finding from Phase 11 (once NAVIGATION_SAFE, once
    LOGIN_TRIGGER across two loads of the SAME URL)."""
    record_attempt("acme", "External", "acme.wd5.myworkdayjobs.com", result="NAVIGATION_SAFE")
    record_attempt("acme", "External", "acme.wd5.myworkdayjobs.com", result="LOGIN_TRIGGER")
    assert classify_stability("acme", "External") == WorkdayStability.VARIABLE


def test_stability_stale_when_too_old(tmp_env):
    from datetime import datetime, timedelta, timezone

    from app.db import db_session

    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    with db_session() as conn:
        conn.execute(
            "INSERT INTO workday_tenant_attempts (tenant, site, host, result, observed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("acme", "External", "acme.wd5.myworkdayjobs.com", "NAVIGATION_SAFE", old),
        )
    assert classify_stability("acme", "External", max_age_days=30) == WorkdayStability.STALE


def test_stability_never_generalizes_across_tenants(tmp_env):
    """CLAUDE.md Phase 12 section 21: acme being STABLE must never imply
    anything about globex."""
    for _ in range(3):
        record_attempt("acme", "External", "acme.wd5.myworkdayjobs.com", result="NAVIGATION_SAFE")
    record_attempt("globex", "External", "globex.wd3.myworkdaysite.com", result="LOGIN_TRIGGER")
    assert classify_stability("acme", "External") == WorkdayStability.STABLE
    assert classify_stability("globex", "External") == WorkdayStability.UNVERIFIED


def test_stability_report_consistent_and_variable_counts(tmp_env):
    record_attempt("acme", "External", "acme.wd5.myworkdayjobs.com", result="NAVIGATION_SAFE")
    record_attempt("acme", "External", "acme.wd5.myworkdayjobs.com", result="NAVIGATION_SAFE")
    record_attempt("acme", "External", "acme.wd5.myworkdayjobs.com", result="LOGIN_TRIGGER")
    report = stability_report()
    acme_summary = next(s for s in report if s.tenant == "acme")
    assert acme_summary.attempt_count == 3
    assert acme_summary.consistent_count == 2
    assert acme_summary.variable_count == 1
    assert acme_summary.stability == WorkdayStability.VARIABLE


def test_list_all_attempts_across_tenants(tmp_env):
    record_attempt("acme", "External", "acme.wd5.myworkdayjobs.com", result="NAVIGATION_SAFE")
    record_attempt("globex", "External", "globex.wd3.myworkdaysite.com", result="LOGIN_TRIGGER")
    all_attempts = list_all_attempts()
    assert len(all_attempts) == 2


def test_cli_workday_stability(tmp_env, capsys):
    from app.applications.cli import main as cli_main

    record_attempt("acme", "External", "acme.wd5.myworkdayjobs.com", result="NAVIGATION_SAFE")
    record_attempt("acme", "External", "acme.wd5.myworkdayjobs.com", result="LOGIN_TRIGGER")
    rc = cli_main(["workday-stability"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "acme/External: VARIABLE" in out


def test_cli_workday_stability_empty_state_honest(tmp_env, capsys):
    from app.applications.cli import main as cli_main

    rc = cli_main(["workday-stability"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "No repeated Workday attempts recorded yet" in out
