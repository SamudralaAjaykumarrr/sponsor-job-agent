import asyncio

import app.agent.scheduler as scheduler_mod
from app.agent import state as agent_state
from app.agent.scheduler import AgentScheduler


def test_scheduler_survives_cycle_failure_and_stops_cleanly(tmp_env, monkeypatch):
    calls = {"count": 0}

    def failing_cycle():
        calls["count"] += 1
        raise RuntimeError("boom")

    monkeypatch.setattr(scheduler_mod, "run_discovery_cycle", failing_cycle)

    agent_state.set_enabled(True)
    try:
        async def run():
            sched = AgentScheduler()
            sched.start()
            await asyncio.sleep(0.3)
            await sched.stop()

        asyncio.run(run())
    finally:
        agent_state.set_enabled(False)

    assert calls["count"] >= 1
    status = agent_state.get_status()
    assert status["last_cycle_summary"] == {"errors": ["cycle crashed -- see server logs"]}


def test_scheduler_no_op_while_disabled(tmp_env, monkeypatch):
    calls = {"count": 0}
    monkeypatch.setattr(scheduler_mod, "run_discovery_cycle", lambda: calls.__setitem__("count", calls["count"] + 1))

    agent_state.set_enabled(False)

    async def run():
        sched = AgentScheduler()
        sched.start()
        await asyncio.sleep(0.2)
        await sched.stop()

    asyncio.run(run())
    assert calls["count"] == 0
