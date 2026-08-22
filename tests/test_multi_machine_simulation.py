"""CLAUDE.md Phase 6 section 23: multi-machine acceptance simulation, run
against both backends. The SQLite variant runs in the default `pytest`
suite (fast, no external service); the PostgreSQL variant is marked
`postgres` and exercises the exact same coordination code against a real
shared Postgres server -- this is where the real overfetch/starvation bug
and the MAX()-is-aggregate-only bug were actually caught during this
build (see app/workers/leasing_postgres.py and app/workers/circuit.py)."""

import pytest

from scripts.multi_machine_simulation import run_simulation


def _assert_simulation_passes(results: dict) -> None:
    lease = results["lease_ownership"]
    assert not lease["errors"], lease["errors"]
    assert lease["no_double_claim"]
    assert lease["hosts_that_claimed_something"] >= 2, "work should spread across more than one simulated host"

    circuit = results["shared_circuit_state"]
    assert circuit["opened_by_host_a"]
    assert circuit["host_b_correctly_blocked"]
    assert circuit["host_c_sees_same_open_state"]

    rate_limit = results["shared_rate_limit"]
    assert rate_limit["never_exceeded_limit"]
    assert rate_limit["slots_granted"] == rate_limit["limit"]

    orphan = results["orphan_recovery"]
    assert orphan["correctly_marked_offline"]


def test_multi_machine_simulation_on_sqlite(tmp_env):
    results = run_simulation()
    _assert_simulation_passes(results)


@pytest.mark.postgres
def test_multi_machine_simulation_on_postgres(pg_fresh_db, monkeypatch):
    import app.db as db

    monkeypatch.setattr(db, "DATABASE_URL", pg_fresh_db)
    results = run_simulation()
    _assert_simulation_passes(results)
