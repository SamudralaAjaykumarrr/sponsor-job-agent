"""CLAUDE.md Phase 15 section 10/84: the global doctor aggregates every
existing subsystem doctor and adds only genuinely new checks (DB/schema,
candidate profile, config, job integrity, dead-letter backlog)."""

from app.doctor import run_global_doctor
from app.jobs_repo import insert_job
from app.models import Job


def test_clean_system_has_no_serious_issues(tmp_env):
    report = run_global_doctor()
    assert report.serious_count == 0
    assert "registry" in report.subsystems_run
    assert "sponsorship" in report.subsystems_run
    assert "applications" in report.subsystems_run
    assert "resume_optimizer" in report.subsystems_run
    assert "candidate_profile" in report.subsystems_run
    assert "config" in report.subsystems_run


def test_flags_blank_title_or_company(tmp_env):
    insert_job(Job(title="", company="", description="x"))
    report = run_global_doctor()
    assert any(i.check == "blank_title_or_company" for i in report.issues)


def test_incomplete_profile_is_a_warning_not_serious(tmp_env):
    # tmp_env's candidate profile is a fresh blank one (all NEEDS_USER_INPUT).
    report = run_global_doctor()
    profile_issues = [i for i in report.issues if i.source == "candidate_profile"]
    assert any(i.check == "profile_incomplete" for i in profile_issues)
    assert all(i.severity == "warning" for i in profile_issues)


def test_one_subsystem_crashing_does_not_abort_the_rest(tmp_env, monkeypatch):
    import app.applications.doctor as applications_doctor_module

    def _boom():
        raise RuntimeError("simulated subsystem failure")

    monkeypatch.setattr(applications_doctor_module, "run_doctor", _boom)
    report = run_global_doctor()
    assert "applications" in report.subsystems_skipped
    assert any(i.source == "applications" and i.check == "doctor_crashed" for i in report.issues)
    # registry/sponsorship/resume_optimizer still ran despite applications crashing.
    assert "registry" in report.subsystems_run
    assert "sponsorship" in report.subsystems_run
    assert "resume_optimizer" in report.subsystems_run


def test_database_unreachable_short_circuits_cleanly(tmp_env, monkeypatch):
    import app.health as health_module

    class _FakeResult:
        ready = False
        database_backend = "sqlite"
        database_reachable = False
        schema_version = 0
        schema_compatible = False
        detail = "simulated unreachable"

    monkeypatch.setattr(health_module, "check_readiness", lambda: _FakeResult())
    report = run_global_doctor()
    assert report.serious_count >= 1
    assert any(i.check == "database_unreachable" for i in report.issues)
    assert "registry" in report.subsystems_skipped
