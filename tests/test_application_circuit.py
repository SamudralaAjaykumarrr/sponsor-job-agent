"""CLAUDE.md Phase 9 section 34: submission circuit breaker -- a SEPARATE
mechanism/table from app.workers.circuit (discovery). Mirrors that module's
own test intent but against app.applications.circuit."""

from app import config
from app.applications import circuit as app_circuit


def test_closed_by_default(tmp_env):
    assert app_circuit.may_attempt("mock_ats") is True
    status = app_circuit.get_status("mock_ats")
    assert status.state == "CLOSED"


def test_consecutive_failures_trip_circuit(tmp_env, monkeypatch):
    monkeypatch.setattr(config, "APPLICATION_CIRCUIT_CONSECUTIVE_TRIP_THRESHOLD", 3)
    for _ in range(3):
        app_circuit.record_result("provider-x", success=False)
    status = app_circuit.get_status("provider-x")
    assert status.state == "OPEN"
    assert app_circuit.may_attempt("provider-x") is False


def test_half_open_probe_after_cooldown_and_recovery(tmp_env, monkeypatch):
    monkeypatch.setattr(config, "APPLICATION_CIRCUIT_CONSECUTIVE_TRIP_THRESHOLD", 1)
    monkeypatch.setattr(config, "APPLICATION_CIRCUIT_BREAKER_COOLDOWN_SECONDS", 0)
    app_circuit.record_result("provider-y", success=False)
    assert app_circuit.get_status("provider-y").state == "OPEN"

    assert app_circuit.may_attempt("provider-y") is True  # cooldown elapsed -> HALF_OPEN probe granted
    assert app_circuit.get_status("provider-y").state == "HALF_OPEN"

    # A second concurrent probe attempt must be refused while one is in flight.
    assert app_circuit.may_attempt("provider-y") is False

    app_circuit.record_result("provider-y", success=True)
    assert app_circuit.get_status("provider-y").state == "CLOSED"


def test_circuit_never_permanently_disables_provider(tmp_env, monkeypatch):
    monkeypatch.setattr(config, "APPLICATION_CIRCUIT_CONSECUTIVE_TRIP_THRESHOLD", 1)
    monkeypatch.setattr(config, "APPLICATION_CIRCUIT_BREAKER_COOLDOWN_SECONDS", 0)
    for _ in range(5):
        app_circuit.record_result("provider-z", success=False)
        assert app_circuit.may_attempt("provider-z") is True  # always eventually probes again


def test_inflight_slot_limit(tmp_env):
    assert app_circuit.acquire_inflight_slot("provider-w", limit=1) is True
    assert app_circuit.acquire_inflight_slot("provider-w", limit=1) is False
    app_circuit.release_inflight_slot("provider-w")
    assert app_circuit.acquire_inflight_slot("provider-w", limit=1) is True


def test_discovery_and_submission_circuits_are_independent(tmp_env):
    from app.workers import circuit as discovery_circuit

    for _ in range(10):
        discovery_circuit.record_result("shared-provider-name", success=False)
    assert discovery_circuit.get_status("shared-provider-name").state == "OPEN"
    # The application submission circuit for the SAME provider name must be
    # completely unaffected -- separate table, separate breaker.
    assert app_circuit.get_status("shared-provider-name").state == "CLOSED"
    assert app_circuit.may_attempt("shared-provider-name") is True


def test_force_close_and_force_probe(tmp_env, monkeypatch):
    monkeypatch.setattr(config, "APPLICATION_CIRCUIT_CONSECUTIVE_TRIP_THRESHOLD", 1)
    app_circuit.record_result("provider-v", success=False)
    assert app_circuit.get_status("provider-v").state == "OPEN"

    app_circuit.force_probe("provider-v")
    assert app_circuit.get_status("provider-v").state == "HALF_OPEN"

    app_circuit.force_close("provider-v")
    status = app_circuit.get_status("provider-v")
    assert status.state == "CLOSED"
    assert status.consecutive_failures == 0
