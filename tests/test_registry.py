import sqlite3

from app.registry.models import CompanyRegistryEntry
from app.registry.repo import (
    get_entry,
    get_entry_by_tenant,
    insert_entry,
    list_due_for_poll,
    list_entries,
    mark_poll_result,
    provider_health_summary,
    update_entry,
)
from app.registry.scheduling import TenantHealth, compute_health, next_interval_minutes


def _entry(**overrides) -> CompanyRegistryEntry:
    defaults = dict(company_name="Acme Corp", provider="greenhouse", tenant_identifier="acme")
    defaults.update(overrides)
    return CompanyRegistryEntry(**defaults)


def test_insert_and_get_entry(tmp_env):
    entry_id = insert_entry(_entry())
    fetched = get_entry(entry_id)
    assert fetched.company_name == "Acme Corp"
    assert fetched.provider == "greenhouse"
    assert fetched.enabled is True


def test_get_entry_by_tenant(tmp_env):
    insert_entry(_entry())
    fetched = get_entry_by_tenant("greenhouse", "acme")
    assert fetched is not None
    assert fetched.company_name == "Acme Corp"
    assert get_entry_by_tenant("greenhouse", "nonexistent") is None


def test_list_entries_filters_by_provider(tmp_env):
    insert_entry(_entry(company_name="A", provider="greenhouse", tenant_identifier="a"))
    insert_entry(_entry(company_name="B", provider="lever", tenant_identifier="b"))
    gh = list_entries(provider="greenhouse")
    assert len(gh) == 1
    assert gh[0].company_name == "A"


def test_list_due_for_poll_includes_never_polled(tmp_env):
    entry_id = insert_entry(_entry())
    due = list_due_for_poll()
    assert any(e.id == entry_id for e in due)


def test_list_due_for_poll_excludes_disabled(tmp_env):
    insert_entry(_entry(enabled=False))
    due = list_due_for_poll()
    assert due == []


def test_mark_poll_result_success_speeds_up_interval_when_yielding_jobs(tmp_env):
    entry_id = insert_entry(_entry(poll_interval_minutes=60))
    mark_poll_result(entry_id, success=True, jobs_new=5, latency_ms=120.0)
    entry = get_entry(entry_id)
    assert entry.poll_interval_minutes < 60
    assert entry.consecutive_failures == 0
    assert entry.last_success_at is not None


def test_mark_poll_result_success_no_new_jobs_slows_down(tmp_env):
    entry_id = insert_entry(_entry(poll_interval_minutes=60))
    mark_poll_result(entry_id, success=True, jobs_new=0, latency_ms=50.0)
    entry = get_entry(entry_id)
    assert entry.poll_interval_minutes > 60


def test_mark_poll_result_failure_backs_off_and_increments_failures(tmp_env):
    entry_id = insert_entry(_entry())
    mark_poll_result(entry_id, success=False, error="connection refused")
    entry = get_entry(entry_id)
    assert entry.consecutive_failures == 1
    assert entry.last_failure_at is not None
    assert entry.last_error == "connection refused"

    mark_poll_result(entry_id, success=False, error="still down")
    entry2 = get_entry(entry_id)
    assert entry2.consecutive_failures == 2
    assert entry2.poll_interval_minutes > entry.poll_interval_minutes  # backs off further


def test_repeated_failures_never_exceed_max_poll_minutes(tmp_env):
    entry_id = insert_entry(_entry())
    for _ in range(15):
        mark_poll_result(entry_id, success=False, error="down")
    entry = get_entry(entry_id)
    from app import config
    assert entry.poll_interval_minutes <= config.PROVIDER_MAX_POLL_MINUTES


def test_interval_never_below_min_poll_minutes(tmp_env):
    entry_id = insert_entry(_entry(poll_interval_minutes=1))
    for _ in range(10):
        mark_poll_result(entry_id, success=True, jobs_new=10)
    entry = get_entry(entry_id)
    from app import config
    assert entry.poll_interval_minutes >= config.PROVIDER_MIN_POLL_MINUTES


def test_compute_health_thresholds():
    assert compute_health(0, "2026-08-21T00:00:00Z") == TenantHealth.HEALTHY
    assert compute_health(3, "2026-08-21T00:00:00Z") == TenantHealth.DEGRADED
    assert compute_health(10, "2026-08-21T00:00:00Z") == TenantHealth.FAILING
    assert compute_health(0, None) == TenantHealth.DEGRADED  # never successfully polled


def test_next_interval_minutes_pure_function():
    from app import config
    assert next_interval_minutes(60, success=True, jobs_new=3, consecutive_failures=0) == max(
        config.PROVIDER_MIN_POLL_MINUTES, int(60 * 0.75))
    assert next_interval_minutes(60, success=True, jobs_new=0, consecutive_failures=0) == min(
        config.PROVIDER_MAX_POLL_MINUTES, int(60 * 1.25))
    assert next_interval_minutes(60, success=False, jobs_new=0, consecutive_failures=1) > 60


def test_provider_health_summary_counts_by_provider(tmp_env):
    healthy_id = insert_entry(_entry(company_name="Healthy Co", tenant_identifier="healthy"))
    mark_poll_result(healthy_id, success=True, jobs_new=1)

    failing_id = insert_entry(_entry(company_name="Failing Co", tenant_identifier="failing"))
    for _ in range(12):
        mark_poll_result(failing_id, success=False, error="down")

    summary = {s["provider"]: s for s in provider_health_summary()}
    assert summary["greenhouse"]["tenants"] == 2
    assert summary["greenhouse"]["healthy"] == 1
    assert summary["greenhouse"]["failing"] == 1


def test_update_entry_is_additive_and_preserves_other_fields(tmp_env):
    entry_id = insert_entry(_entry(notes="original"))
    update_entry(entry_id, notes="updated")
    entry = get_entry(entry_id)
    assert entry.notes == "updated"
    assert entry.company_name == "Acme Corp"  # untouched


def test_registry_table_created_by_init_db_migration(tmp_env):
    """DB migration safety: company_registry/job_provenance/discovery_log
    tables must exist after init_db() without destroying anything else."""
    import app.db as db
    with db.db_session() as conn:
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    assert "company_registry" in tables
    assert "job_provenance" in tables
    assert "discovery_log" in tables


def test_migration_preserves_existing_jobs_and_state(tmp_env, sample_profile):
    """Before/after migration: running init_db() again on an existing DB with
    data must not destroy or alter existing jobs/discovery_cycles rows."""
    import app.db as db
    from app.candidate.profile import save_profile
    from app.jobs_repo import insert_discovery_cycle
    from app.models import Job

    save_profile(sample_profile)
    job = Job(title="Existing Job", company="Acme", description="desc")
    from app.jobs_repo import insert_job
    job_id = insert_job(job)
    insert_discovery_cycle({"started_at": "2026-08-01T00:00:00Z", "finished_at": "2026-08-01T00:01:00Z",
                             "providers": ["greenhouse"], "jobs_fetched": 1, "errors": []})

    # Re-run migrations (simulates restarting the app after upgrading).
    db.init_db()
    db.init_db()

    from app.jobs_repo import get_job, list_discovery_cycles
    preserved = get_job(job_id)
    assert preserved is not None
    assert preserved.title == "Existing Job"
    cycles = list_discovery_cycles()
    assert len(cycles) == 1
