"""CLAUDE.md Phase 10 section 43: the reconciliation pass and the stale
browser-assist session reaper both actually run on a schedule now (Phase 9
only ever defined the config flags -- nothing read them until this
module). Mirrors tests/test_agent_scheduler.py's plain `asyncio.run(...)`
pattern (pytest-asyncio isn't a project dependency)."""

import asyncio

import app.applications.background_scheduler as bg_mod
from app import config
from app.applications.background_scheduler import ApplicationBackgroundScheduler


def _run(coro) -> None:
    asyncio.run(coro())


def test_start_and_stop_is_clean(tmp_env, monkeypatch):
    monkeypatch.setattr(config, "RECONCILE_WORKER_ENABLED", False)
    monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", False)

    sched = ApplicationBackgroundScheduler()

    async def go():
        sched.start()
        await asyncio.sleep(0.05)
        await sched.stop()

    _run(go)
    assert sched._task.done()


def test_reconcile_pass_runs_when_enabled(tmp_env, monkeypatch):
    calls = []
    monkeypatch.setattr(config, "RECONCILE_WORKER_ENABLED", True)
    monkeypatch.setattr(config, "RECONCILE_WORKER_INTERVAL_SECONDS", 60)
    monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", False)
    monkeypatch.setattr(bg_mod, "_run_reconcile_pass", lambda: calls.append("reconcile"))
    monkeypatch.setattr(bg_mod, "_IDLE_POLL_SECONDS", 0.01)

    sched = ApplicationBackgroundScheduler()

    async def go():
        sched.start()
        await asyncio.sleep(0.1)
        await sched.stop()

    _run(go)
    assert calls == ["reconcile"]  # ran exactly once in this short window, not once per idle tick


def test_stale_session_reap_runs_when_browser_assist_enabled(tmp_env, monkeypatch):
    calls = []
    monkeypatch.setattr(config, "RECONCILE_WORKER_ENABLED", False)
    monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", True)
    monkeypatch.setattr(config, "BROWSER_SESSION_TIMEOUT_MINUTES", 30)
    monkeypatch.setattr(bg_mod, "_run_stale_session_reap", lambda: calls.append("reap"))
    monkeypatch.setattr(bg_mod, "_IDLE_POLL_SECONDS", 0.01)

    sched = ApplicationBackgroundScheduler()

    async def go():
        sched.start()
        await asyncio.sleep(0.1)
        await sched.stop()

    _run(go)
    assert calls == ["reap"]


def test_reconcile_does_not_run_when_disabled(tmp_env, monkeypatch):
    monkeypatch.setattr(config, "RECONCILE_WORKER_ENABLED", False)
    monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", False)
    reconcile_calls, reap_calls = [], []
    monkeypatch.setattr(bg_mod, "_run_reconcile_pass", lambda: reconcile_calls.append(1))
    monkeypatch.setattr(bg_mod, "_run_stale_session_reap", lambda: reap_calls.append(1))
    monkeypatch.setattr(bg_mod, "_IDLE_POLL_SECONDS", 0.01)

    sched = ApplicationBackgroundScheduler()

    async def go():
        sched.start()
        await asyncio.sleep(0.1)
        await sched.stop()

    _run(go)
    assert reconcile_calls == []
    assert reap_calls == [1]  # tsenta-parity-closure-v1: reap is unconditional, unlike reconcile


def test_stale_session_reap_runs_even_when_browser_assist_disabled(tmp_env, monkeypatch):
    """Tsenta-parity-closure-v1 regression test (scenario A): cleanup of an
    already-open session must never depend on whether NEW sessions are
    currently allowed to be created. Real bug caught during the audit --
    the reap branch used to be gated on the same BROWSER_ASSIST_ENABLED flag
    that gates session creation, leaving a stale session (job 327) stuck
    active=1 forever once the flag reverted to its default (False)."""
    monkeypatch.setattr(config, "RECONCILE_WORKER_ENABLED", False)
    monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", False)
    monkeypatch.setattr(config, "BROWSER_SESSION_TIMEOUT_MINUTES", 30)
    reap_calls = []
    monkeypatch.setattr(bg_mod, "_run_stale_session_reap", lambda: reap_calls.append(1))
    monkeypatch.setattr(bg_mod, "_IDLE_POLL_SECONDS", 0.01)

    sched = ApplicationBackgroundScheduler()

    async def go():
        sched.start()
        await asyncio.sleep(0.1)
        await sched.stop()

    _run(go)
    assert reap_calls == [1]


def test_one_failing_task_never_stops_the_loop(tmp_env, monkeypatch):
    monkeypatch.setattr(config, "RECONCILE_WORKER_ENABLED", True)
    monkeypatch.setattr(config, "RECONCILE_WORKER_INTERVAL_SECONDS", 60)
    monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", True)
    monkeypatch.setattr(config, "BROWSER_SESSION_TIMEOUT_MINUTES", 30)

    reap_calls = []

    def _boom():
        raise RuntimeError("reconcile pass exploded")

    monkeypatch.setattr(bg_mod, "_run_reconcile_pass", _boom)
    monkeypatch.setattr(bg_mod, "_run_stale_session_reap", lambda: reap_calls.append(1))
    monkeypatch.setattr(bg_mod, "_IDLE_POLL_SECONDS", 0.01)

    sched = ApplicationBackgroundScheduler()

    async def go():
        sched.start()
        await asyncio.sleep(0.1)
        await sched.stop()

    _run(go)
    assert reap_calls == [1]
