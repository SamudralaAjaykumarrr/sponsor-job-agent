"""CLAUDE.md Phase 11 sections 42-43: dated capability-evidence tracking and
staleness. Never touches network/browser."""

from datetime import datetime, timedelta, timezone

from app import config
from app.applications.capability_evidence import (
    EvidenceVerificationType,
    evidence_age_days,
    get_evidence,
    is_stale,
    list_evidence,
    list_stale,
    record_evidence,
)


def test_record_and_get_evidence(tmp_env):
    record_evidence("smartrecruiters", "apply_first_click", EvidenceVerificationType.LIVE_PUBLIC,
                     notes="observed live", source_domain="jobs.smartrecruiters.com")
    row = get_evidence("smartrecruiters", "apply_first_click")
    assert row["verification_type"] == "LIVE_PUBLIC"
    assert row["source_domain"] == "jobs.smartrecruiters.com"


def test_record_evidence_upserts_single_row(tmp_env):
    record_evidence("workable", "field_discovery", EvidenceVerificationType.NOT_TESTED)
    record_evidence("workable", "field_discovery", EvidenceVerificationType.LIVE_PUBLIC, notes="found a tenant")
    rows = list_evidence(provider="workable")
    assert len(rows) == 1
    assert rows[0]["verification_type"] == "LIVE_PUBLIC"


def test_not_tested_and_fixture_never_stale(tmp_env):
    record_evidence("bamboohr", "field_discovery", EvidenceVerificationType.NOT_TESTED)
    record_evidence("mock_ats", "field_discovery", EvidenceVerificationType.FIXTURE)
    assert is_stale(get_evidence("bamboohr", "field_discovery")) is False
    assert is_stale(get_evidence("mock_ats", "field_discovery")) is False


def test_live_public_evidence_becomes_stale_after_max_age(tmp_env, monkeypatch):
    old_observed = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    record_evidence("greenhouse", "field_discovery", EvidenceVerificationType.LIVE_PUBLIC, observed_at=old_observed)
    row = get_evidence("greenhouse", "field_discovery")
    assert is_stale(row, max_age_days=30) is True
    assert is_stale(row, max_age_days=90) is False


def test_default_max_age_from_config(tmp_env, monkeypatch):
    monkeypatch.setattr(config, "CAPABILITY_EVIDENCE_MAX_AGE_DAYS", 1)
    old_observed = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    record_evidence("lever", "field_discovery", EvidenceVerificationType.LIVE_PUBLIC, observed_at=old_observed)
    row = get_evidence("lever", "field_discovery")
    assert is_stale(row) is True


def test_list_stale_only_returns_stale_rows(tmp_env):
    fresh = record_evidence("ashby", "field_discovery", EvidenceVerificationType.LIVE_PUBLIC)
    old_observed = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    record_evidence("greenhouse", "field_discovery", EvidenceVerificationType.LIVE_PUBLIC, observed_at=old_observed)
    stale = list_stale(max_age_days=30)
    stale_providers = {r.row["provider"] for r in stale}
    assert "greenhouse" in stale_providers
    assert "ashby" not in stale_providers


def test_evidence_age_days_handles_bad_timestamp():
    assert evidence_age_days("not-a-timestamp") == float("inf")


# --- CLAUDE.md Phase 12 section 41: repeated REAL_BROWSER evidence ------------

def test_real_browser_repeat_count_starts_at_one(tmp_env):
    row = record_evidence("smartrecruiters", "apply_first_click", EvidenceVerificationType.REAL_BROWSER)
    assert row["repeat_count"] == 1
    assert row["verification_type"] == "REAL_BROWSER"


def test_second_real_browser_observation_promotes_to_repeated(tmp_env):
    record_evidence("smartrecruiters", "apply_first_click", EvidenceVerificationType.REAL_BROWSER)
    row = record_evidence("smartrecruiters", "apply_first_click", EvidenceVerificationType.REAL_BROWSER)
    assert row["repeat_count"] == 2
    assert row["verification_type"] == "REAL_BROWSER_REPEATED"


def test_third_real_browser_observation_keeps_incrementing(tmp_env):
    record_evidence("smartrecruiters", "apply_first_click", EvidenceVerificationType.REAL_BROWSER)
    record_evidence("smartrecruiters", "apply_first_click", EvidenceVerificationType.REAL_BROWSER)
    row = record_evidence("smartrecruiters", "apply_first_click", EvidenceVerificationType.REAL_BROWSER_REPEATED)
    assert row["repeat_count"] == 3


def test_fixture_observation_resets_repeat_streak(tmp_env):
    """A weaker (FIXTURE/NOT_TESTED/STATIC_HTML) re-check never counts
    toward, and resets, the real-browser repeat streak."""
    record_evidence("smartrecruiters", "apply_first_click", EvidenceVerificationType.REAL_BROWSER)
    record_evidence("smartrecruiters", "apply_first_click", EvidenceVerificationType.REAL_BROWSER)
    row = record_evidence("smartrecruiters", "apply_first_click", EvidenceVerificationType.NOT_TESTED)
    assert row["repeat_count"] == 1
    assert row["verification_type"] == "NOT_TESTED"


def test_static_html_and_real_browser_are_time_sensitive_for_staleness(tmp_env):
    old_observed = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    record_evidence("smartrecruiters", "landing_navigation", EvidenceVerificationType.REAL_BROWSER,
                     observed_at=old_observed)
    row = get_evidence("smartrecruiters", "landing_navigation")
    assert is_stale(row, max_age_days=30) is True

    record_evidence("smartrecruiters", "field_discovery", EvidenceVerificationType.STATIC_HTML,
                     observed_at=old_observed)
    row2 = get_evidence("smartrecruiters", "field_discovery")
    assert is_stale(row2, max_age_days=30) is False
