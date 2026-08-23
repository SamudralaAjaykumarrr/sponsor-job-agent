"""Agent orchestrator doctor checks (CLAUDE.md one-click-agent section 47)."""

from app.agent import doctor as agent_doctor
from app.agent import run_state
from app.agent.run_state import AgentRunState


def test_doctor_clean_on_fresh_stopped_state(tmp_env):
    report = agent_doctor.run_doctor()
    assert report.serious_count == 0


def test_running_but_loop_absent_is_flagged(tmp_env):
    run_state.set_actual_state(AgentRunState.RUNNING)
    try:
        report = agent_doctor.run_doctor()
        assert any(i.check == "agent_running_but_loop_absent" for i in report.issues)
    finally:
        run_state.set_actual_state(AgentRunState.STOPPED)


def test_auto_prepare_safety_gate_static_check_passes(tmp_env):
    report = agent_doctor.run_doctor()
    assert not any(i.check == "auto_prepare_without_safety_gates" for i in report.issues)


def test_global_doctor_includes_agent_subsystem(tmp_env):
    from app.doctor import run_global_doctor

    report = run_global_doctor()
    assert "agent" in report.subsystems_run
