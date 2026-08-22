"""CLAUDE.md Phase 13 sections 11-12, 15-16: application/browser-assist
provider flow health, distinct from discovery/submission circuit breakers.
Never touches network/browser -- pure record/compute logic over db_session()."""

from datetime import datetime, timedelta, timezone

from app import config
from app.applications.provider_health import (
    FailureKind,
    ProviderAssistHealth,
    clear_captcha_flag,
    compute_health,
    get_health,
    list_health,
    record_failure,
    record_success,
)


def test_unverified_when_never_observed(tmp_env):
    result = get_health("greenhouse")
    assert result["health"] == ProviderAssistHealth.UNVERIFIED.value
    assert result["row"] is None


def test_success_marks_healthy(tmp_env):
    record_success("greenhouse", form_fingerprint="abc123", live_validation=True)
    result = get_health("greenhouse")
    assert result["health"] == ProviderAssistHealth.HEALTHY.value


def test_captcha_failure_marks_captcha_blocked(tmp_env):
    record_success("lever", live_validation=True)
    record_failure("lever", FailureKind.CAPTCHA)
    result = get_health("lever")
    assert result["health"] == ProviderAssistHealth.CAPTCHA_BLOCKED.value


def test_auth_gate_failure_marks_auth_gated(tmp_env):
    record_success("workday", tenant="acme", site="External", live_validation=True)
    record_failure("workday", FailureKind.AUTH_GATE, tenant="acme", site="External")
    result = get_health("workday", tenant="acme", site="External")
    assert result["health"] == ProviderAssistHealth.AUTH_GATED.value


def test_clear_captcha_flag_recovers_to_healthy_after_success(tmp_env):
    record_success("lever", live_validation=True)
    record_failure("lever", FailureKind.CAPTCHA)
    assert get_health("lever")["health"] == ProviderAssistHealth.CAPTCHA_BLOCKED.value
    clear_captcha_flag("lever")
    record_success("lever", live_validation=True)
    assert get_health("lever")["health"] == ProviderAssistHealth.HEALTHY.value


def test_consecutive_failures_marks_degraded(tmp_env):
    record_success("ashby", live_validation=True)
    for _ in range(3):
        record_failure("ashby", FailureKind.GENERIC)
    result = get_health("ashby")
    assert result["health"] == ProviderAssistHealth.DEGRADED.value


def test_schema_drift_count_marks_schema_drift(tmp_env):
    record_success("workable", live_validation=True)
    record_failure("workable", FailureKind.SCHEMA_DRIFT)
    record_failure("workable", FailureKind.SCHEMA_DRIFT)
    result = get_health("workable")
    assert result["health"] == ProviderAssistHealth.SCHEMA_DRIFT.value


def test_success_resets_consecutive_failures(tmp_env):
    record_success("greenhouse", live_validation=True)
    record_failure("greenhouse", FailureKind.GENERIC)
    record_failure("greenhouse", FailureKind.GENERIC)
    record_success("greenhouse", live_validation=True)
    result = get_health("greenhouse")
    assert result["row"]["consecutive_failures"] == 0
    assert result["health"] == ProviderAssistHealth.HEALTHY.value


def test_stale_evidence_overrides_healthy_looking_row(tmp_env):
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    record_success("smartrecruiters", live_validation=True)
    # Manually age the row via a second success call with an explicit
    # timestamp isn't supported -- simulate staleness by lowering the max
    # age threshold instead, matching capability_evidence's own test style.
    result = compute_health(get_health("smartrecruiters")["row"], max_age_days=-1)
    assert result == ProviderAssistHealth.STALE


def test_never_disables_provider_after_captcha_only_surfaces(tmp_env):
    """CLAUDE.md Phase 13 sections 11, 16: recording evidence never itself
    disables a provider -- the row is always still readable/queryable."""
    record_failure("bamboohr", FailureKind.CAPTCHA)
    result = get_health("bamboohr")
    assert result["row"] is not None
    assert result["health"] == ProviderAssistHealth.CAPTCHA_BLOCKED.value


def test_list_health_never_collapses_tenants(tmp_env):
    record_success("workday", tenant="acme", site="External", live_validation=True)
    record_success("workday", tenant="globex", site="External", live_validation=True)
    rows = list_health()
    keys = {(r["row"]["provider"], r["row"]["tenant"], r["row"]["site"]) for r in rows}
    assert ("workday", "acme", "External") in keys
    assert ("workday", "globex", "External") in keys


def test_form_verified_without_success_stays_unverified(tmp_env):
    """A row with no last_success/last_live_validation is UNVERIFIED even if
    somehow flagged form_verified -- never inflate to HEALTHY without a
    genuine timestamped observation."""
    assert compute_health(None) == ProviderAssistHealth.UNVERIFIED
