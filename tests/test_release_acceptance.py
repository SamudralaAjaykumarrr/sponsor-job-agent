"""CLAUDE.md Phase 15 sections 81-82: release acceptance runner. Tests the
lightweight, always-safe checks directly; the pytest-subprocess checks are
exercised via mocking (running pytest inside pytest would be wasteful/
recursive) but their result-interpretation logic is still verified."""

import app.acceptance as acceptance


def test_lightweight_checks_pass_on_clean_repo(tmp_env):
    report = acceptance.AcceptanceReport()
    acceptance.check_compile(report)
    acceptance.check_gitignore_coverage(report)
    acceptance.check_secret_scan(report)
    acceptance.check_fresh_sqlite_migration(report)
    assert report.ok
    assert all(c.status == "PASS" for c in report.checks)


def test_fresh_sqlite_migration_never_touches_real_db(tmp_env):
    """Uses tmp_env's isolated DB_PATH -- proves the check creates its own
    throwaway file rather than reading/writing config.DB_PATH."""
    import app.config as config

    before = config.DB_PATH.exists()
    report = acceptance.AcceptanceReport()
    acceptance.check_fresh_sqlite_migration(report)
    after = config.DB_PATH.exists()
    assert before == after
    assert report.checks[0].status == "PASS"


def test_gitignore_missing_pattern_is_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(acceptance, "REPO_ROOT", tmp_path)
    (tmp_path / ".gitignore").write_text("*.pyc\n")
    report = acceptance.AcceptanceReport()
    acceptance.check_gitignore_coverage(report)
    assert report.checks[0].status == "FAIL"
    assert not report.ok


def test_report_ok_is_false_when_any_check_fails():
    report = acceptance.AcceptanceReport()
    report.checks.append(acceptance.CheckResult("a", "PASS"))
    report.checks.append(acceptance.CheckResult("b", "FAIL", "boom"))
    assert report.ok is False


def test_report_ok_ignores_skipped():
    report = acceptance.AcceptanceReport()
    report.checks.append(acceptance.CheckResult("a", "PASS"))
    report.checks.append(acceptance.CheckResult("b", "SKIPPED", "no pgserver"))
    assert report.ok is True


def test_pytest_result_interpretation_pass(monkeypatch):
    monkeypatch.setattr(acceptance, "_run", lambda cmd, timeout=900: (0, "5 passed in 0.1s"))
    report = acceptance.AcceptanceReport()
    acceptance.check_default_pytest(report)
    assert report.checks[0].status == "PASS"


def test_pytest_result_interpretation_fail(monkeypatch):
    monkeypatch.setattr(acceptance, "_run", lambda cmd, timeout=900: (1, "1 failed, 4 passed in 0.1s"))
    report = acceptance.AcceptanceReport()
    acceptance.check_default_pytest(report)
    assert report.checks[0].status == "FAIL"


def test_postgres_check_skips_honestly_when_pgserver_missing(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *a, **kw):
        if name == "pgserver":
            raise ImportError("simulated missing pgserver")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    report = acceptance.AcceptanceReport()
    acceptance.check_postgres_pytest(report)
    assert report.checks[0].status == "SKIPPED"
    assert "pgserver" in report.checks[0].detail


def test_global_doctor_check_fails_on_serious_issue(monkeypatch):
    class _FakeReport:
        serious_count = 1
        warning_count = 0
        subsystems_run = ["x"]

    import app.doctor as doctor_module

    monkeypatch.setattr(doctor_module, "run_global_doctor", lambda: _FakeReport())
    report = acceptance.AcceptanceReport()
    acceptance.check_global_doctor(report)
    assert report.checks[0].status == "FAIL"
