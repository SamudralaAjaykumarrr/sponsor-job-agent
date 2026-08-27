"""Autonomous-ux-reliability-v1 section G: the calm 5-state projection over
the orchestrator's real (actual_state, desired_state, run-history) fields.
Purely a derived read -- never a second persisted state machine."""

from app.agent.run_state import AgentRunState, display_state


def _state(**overrides) -> dict:
    base = {
        "actual_state": AgentRunState.STOPPED.value, "desired_state": AgentRunState.STOPPED.value,
        "started_at": None, "cycle_number": 0, "last_cycle_started_at": None,
    }
    base.update(overrides)
    return base


def test_running_when_actually_running():
    assert display_state(_state(actual_state="RUNNING", desired_state="RUNNING")) == "RUNNING"


def test_idle_when_never_started():
    assert display_state(_state()) == "IDLE"


def test_paused_by_user_after_explicit_stop_with_prior_history():
    s = _state(actual_state="STOPPED", desired_state="STOPPED", started_at="2026-01-01T00:00:00+00:00")
    assert display_state(s) == "PAUSED_BY_USER"


def test_stopped_when_halted_despite_desired_running():
    """Lease lost / crash -- desired_state never flipped to STOPPED, so this
    is an unexpected halt, not a deliberate user pause."""
    s = _state(actual_state="STOPPED", desired_state="RUNNING", started_at="2026-01-01T00:00:00+00:00")
    assert display_state(s) == "STOPPED"


def test_error_state_shows_as_stopped():
    s = _state(actual_state="ERROR", desired_state="RUNNING")
    assert display_state(s) == "STOPPED"


def test_recovering_when_starting_with_prior_cycle_history():
    s = _state(actual_state="STARTING", desired_state="RUNNING", cycle_number=3,
               last_cycle_started_at="2026-01-01T00:00:00+00:00")
    assert display_state(s) == "RECOVERING"


def test_running_when_starting_fresh_with_no_history():
    s = _state(actual_state="STARTING", desired_state="RUNNING")
    assert display_state(s) == "RUNNING"


def test_stopping_after_user_stop_shows_paused_by_user():
    s = _state(actual_state="STOPPING", desired_state="STOPPED", started_at="2026-01-01T00:00:00+00:00")
    assert display_state(s) == "PAUSED_BY_USER"
