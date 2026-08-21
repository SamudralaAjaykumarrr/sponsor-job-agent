import httpx

from app import config
from app.providers.http_client import ProviderHTTPError
from app.workers import circuit, retry


def test_classify_permanent_http_errors():
    for code in ("400", "401", "403", "404", "410"):
        exc = ProviderHTTPError("greenhouse", f"HTTP {code}: not found")
        retryable, error_type = retry.classify_exception(exc)
        assert retryable is False
        assert error_type == "permanent_http_error"


def test_classify_temporary_http_errors():
    for code in ("429", "500", "502", "503", "504"):
        exc = ProviderHTTPError("greenhouse", f"HTTP {code} after 3 attempt(s)")
        retryable, error_type = retry.classify_exception(exc)
        assert retryable is True
        assert error_type == "temporary_http_error"


def test_classify_unrecognized_error_is_conservatively_retryable():
    exc = ProviderHTTPError("greenhouse", "something completely unexpected")
    retryable, error_type = retry.classify_exception(exc)
    assert retryable is True
    assert error_type == "unclassified_http_error"


def test_classify_network_exceptions_are_retryable():
    retryable, _ = retry.classify_exception(httpx.ConnectTimeout("timed out"))
    assert retryable is True
    retryable, _ = retry.classify_exception(httpx.ConnectError("refused"))
    assert retryable is True


def test_backoff_is_bounded_exponential():
    delays = [retry.backoff_seconds(n, base_seconds=10, cap_seconds=100) for n in range(1, 8)]
    assert delays == [10, 20, 40, 80, 100, 100, 100]  # caps at 100


# --- circuit breaker ---------------------------------------------------

def test_circuit_starts_closed(tmp_env):
    status = circuit.get_status("greenhouse")
    assert status.state == "CLOSED"
    assert circuit.may_attempt("greenhouse") is True


def test_circuit_trips_open_after_consecutive_failures(tmp_env, monkeypatch):
    monkeypatch.setattr(config, "CIRCUIT_BREAKER_FAILURE_THRESHOLD", 0.5)
    for _ in range(5):
        circuit.record_result("flaky", success=False)
    status = circuit.get_status("flaky")
    assert status.state == "OPEN"
    assert circuit.may_attempt("flaky") is False


def test_circuit_does_not_trip_on_isolated_failures_below_threshold(tmp_env):
    circuit.record_result("mostly-healthy", success=True)
    circuit.record_result("mostly-healthy", success=True)
    circuit.record_result("mostly-healthy", success=False)
    circuit.record_result("mostly-healthy", success=True)
    circuit.record_result("mostly-healthy", success=True)
    status = circuit.get_status("mostly-healthy")
    assert status.state == "CLOSED"
    assert circuit.may_attempt("mostly-healthy") is True


def test_circuit_half_open_after_cooldown_and_closes_on_success(tmp_env, monkeypatch):
    monkeypatch.setattr(config, "CIRCUIT_BREAKER_COOLDOWN_SECONDS", 0)
    for _ in range(5):
        circuit.record_result("recovering", success=False)
    assert circuit.get_status("recovering").state == "OPEN"

    # Cooldown is 0s -- may_attempt() should transition OPEN -> HALF_OPEN and
    # grant exactly one probe slot.
    assert circuit.may_attempt("recovering") is True
    assert circuit.get_status("recovering").state == "HALF_OPEN"
    # A second concurrent probe attempt must NOT be granted while one is in flight.
    assert circuit.may_attempt("recovering") is False

    circuit.record_result("recovering", success=True)
    status = circuit.get_status("recovering")
    assert status.state == "CLOSED"
    assert status.consecutive_failures == 0


def test_circuit_half_open_reopens_on_probe_failure(tmp_env, monkeypatch):
    monkeypatch.setattr(config, "CIRCUIT_BREAKER_COOLDOWN_SECONDS", 0)
    for _ in range(5):
        circuit.record_result("still-broken", success=False)
    assert circuit.may_attempt("still-broken") is True  # -> HALF_OPEN, probe granted
    circuit.record_result("still-broken", success=False)
    status = circuit.get_status("still-broken")
    assert status.state == "OPEN"


def test_circuit_never_permanently_disables_a_provider(tmp_env, monkeypatch):
    """No matter how many times a provider fails, may_attempt() must always
    eventually return True again once the cooldown elapses -- never a
    one-way permanent trip."""
    monkeypatch.setattr(config, "CIRCUIT_BREAKER_COOLDOWN_SECONDS", 0)
    for cycle in range(3):
        for _ in range(5):
            circuit.record_result("chronic", success=False)
        assert circuit.may_attempt("chronic") is True  # always gets another chance
        circuit.record_result("chronic", success=False)  # probe fails again


def test_circuit_is_isolated_per_provider(tmp_env):
    for _ in range(6):
        circuit.record_result("bad-provider", success=False)
    circuit.record_result("good-provider", success=True)
    assert circuit.may_attempt("bad-provider") is False
    assert circuit.may_attempt("good-provider") is True


def test_release_half_open_probe_frees_stuck_slot(tmp_env, monkeypatch):
    monkeypatch.setattr(config, "CIRCUIT_BREAKER_COOLDOWN_SECONDS", 0)
    for _ in range(5):
        circuit.record_result("p", success=False)
    assert circuit.may_attempt("p") is True  # claims the HALF_OPEN probe slot
    assert circuit.may_attempt("p") is False  # slot held
    circuit.release_half_open_probe("p")
    assert circuit.may_attempt("p") is True  # freed, another probe can proceed


# --- provider concurrency (inflight slots) -----------------------------

def test_inflight_slot_limit_is_enforced(tmp_env):
    assert circuit.acquire_inflight_slot("greenhouse", limit=2) is True
    assert circuit.acquire_inflight_slot("greenhouse", limit=2) is True
    assert circuit.acquire_inflight_slot("greenhouse", limit=2) is False  # at limit


def test_inflight_slot_release_frees_capacity(tmp_env):
    circuit.acquire_inflight_slot("greenhouse", limit=1)
    assert circuit.acquire_inflight_slot("greenhouse", limit=1) is False
    circuit.release_inflight_slot("greenhouse")
    assert circuit.acquire_inflight_slot("greenhouse", limit=1) is True


def test_inflight_slots_are_isolated_per_provider(tmp_env):
    circuit.acquire_inflight_slot("greenhouse", limit=1)
    assert circuit.acquire_inflight_slot("lever", limit=1) is True  # unaffected by greenhouse being full


def test_reset_inflight_slots_recovers_from_stranded_counter(tmp_env):
    circuit.acquire_inflight_slot("greenhouse", limit=1)  # never released (simulated crash)
    assert circuit.acquire_inflight_slot("greenhouse", limit=1) is False
    circuit.reset_inflight_slots("greenhouse")
    assert circuit.acquire_inflight_slot("greenhouse", limit=1) is True
