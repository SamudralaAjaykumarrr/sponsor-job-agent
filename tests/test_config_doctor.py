"""CLAUDE.md Phase 15 section 11: configuration validation, never printing a
secret value."""

from app.config_doctor import _redact_database_url, run_config_doctor


def test_clean_default_config_has_no_serious_issues(tmp_env, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    report = run_config_doctor()
    assert report.serious_count == 0


def test_unrecognized_database_url_is_serious(tmp_env, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "mysql://user:pass@host/db")
    report = run_config_doctor()
    assert any(i.check == "database_url_unrecognized" for i in report.issues)
    assert report.serious_count >= 1


def test_database_url_credentials_never_echoed_raw(tmp_env, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "mysql://user:supersecretpassword@host/db")
    report = run_config_doctor()
    for issue in report.issues:
        assert "supersecretpassword" not in issue.detail


def test_redact_database_url_masks_password():
    assert _redact_database_url("postgresql://sponsor:hunter2@db:5432/x") == "postgresql://sponsor:***@db:5432/x"


def test_auto_submit_without_executor_is_serious(tmp_env, monkeypatch):
    import app.config as config

    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", True)
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", False)
    report = run_config_doctor()
    assert any(i.check == "auto_submit_without_executor" for i in report.issues)


def test_invalid_shard_index_is_serious(tmp_env, monkeypatch):
    import app.config as config

    monkeypatch.setattr(config, "REGISTRY_SHARD_COUNT", 2)
    monkeypatch.setattr(config, "REGISTRY_SHARD_INDEX", 5)
    report = run_config_doctor()
    assert any(i.check == "invalid_shard_index" for i in report.issues)


def test_invalid_identity_confidence_is_serious(tmp_env, monkeypatch):
    import app.config as config

    monkeypatch.setattr(config, "APPLICATION_IDENTITY_MIN_CONFIDENCE", "NONSENSE")
    report = run_config_doctor()
    assert any(i.check == "invalid_identity_confidence" for i in report.issues)


def test_rate_limit_company_exceeds_daily_is_warning(tmp_env, monkeypatch):
    import app.config as config

    monkeypatch.setattr(config, "MAX_APPLICATIONS_PER_DAY", 5)
    monkeypatch.setattr(config, "MAX_APPLICATIONS_PER_COMPANY_PER_DAY", 10)
    report = run_config_doctor()
    issue = next(i for i in report.issues if i.check == "rate_limit_company_exceeds_daily")
    assert issue.severity == "warning"
