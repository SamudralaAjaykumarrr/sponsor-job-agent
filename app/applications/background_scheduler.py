"""CLAUDE.md Phase 10 section 43: upgrades the Phase 9 reconciliation
FOUNDATION (app.applications.reconcile_worker.run_pass(), previously only
ever invoked manually via the CLI/dashboard "run once" button) into an
actual scheduled background operation -- RECONCILE_WORKER_ENABLED and
RECONCILE_WORKER_INTERVAL_SECONDS were defined in app.config back in Phase 9
but nothing ever read them until now. Also runs the Phase 10 section 50
stale browser-assist session reaper on its own independent cadence.

Mirrors app.agent.scheduler.AgentScheduler's structure exactly: a
lightweight asyncio loop owned by the FastAPI app's lifespan, with the
actual (synchronous, DB-querying) work run via asyncio.to_thread so it never
blocks the event loop or any concurrent dashboard request. Both sub-tasks
are independently gated by their own existing flags/intervals and a failure
in one must never stop the other or crash the loop -- same "one thing
failing must never take down the rest" principle as every other background
loop in this project."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app import config

logger = logging.getLogger("applications.background_scheduler")

_IDLE_POLL_SECONDS = 30


def _run_reconcile_pass() -> None:
    from app.applications.reconcile_worker import run_pass

    result = run_pass()
    logger.info(
        "scheduled reconciliation pass: checked=%s auto_resolved_applied=%s auto_resolved_not_submitted=%s "
        "unsupported_provider=%s still_unknown=%s errors=%s",
        result.checked, result.auto_resolved_applied, result.auto_resolved_not_submitted,
        result.unsupported_provider, result.still_unknown, len(result.errors),
    )


def _run_stale_session_reap() -> None:
    from app.applications.browser_assist import expire_stale_sessions

    expired = expire_stale_sessions()
    if expired:
        logger.info("scheduled browser-assist stale-session reap: expired %s session(s)", len(expired))


class ApplicationBackgroundScheduler:
    """Background asyncio loop for two independent, low-frequency
    maintenance tasks. A module-level singleton is reused across the app's
    lifespan, same as app.agent.scheduler.scheduler."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._next_reconcile_at: datetime | None = None
        self._next_reap_at: datetime | None = None

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._next_reconcile_at = None
        self._next_reap_at = None
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if not self._task:
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=5)
        except asyncio.TimeoutError:
            self._task.cancel()

    async def _loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            now = datetime.now(timezone.utc)

            if config.RECONCILE_WORKER_ENABLED and (self._next_reconcile_at is None or now >= self._next_reconcile_at):
                try:
                    await asyncio.to_thread(_run_reconcile_pass)
                except Exception:  # noqa: BLE001 -- one bad pass must never kill the loop
                    logger.exception("scheduled reconciliation pass failed unexpectedly")
                interval = max(60, config.RECONCILE_WORKER_INTERVAL_SECONDS)
                self._next_reconcile_at = datetime.now(timezone.utc) + timedelta(seconds=interval)

            if config.BROWSER_ASSIST_ENABLED and (self._next_reap_at is None or now >= self._next_reap_at):
                try:
                    await asyncio.to_thread(_run_stale_session_reap)
                except Exception:  # noqa: BLE001
                    logger.exception("scheduled browser-assist stale-session reap failed unexpectedly")
                # Reap at roughly half the session timeout so an abandoned
                # session is never left ACTIVE for much longer than the
                # configured timeout actually promises.
                interval = max(60, (config.BROWSER_SESSION_TIMEOUT_MINUTES * 60) // 2)
                self._next_reap_at = datetime.now(timezone.utc) + timedelta(seconds=interval)

            await self._wait(_IDLE_POLL_SECONDS)

    async def _wait(self, seconds: float) -> None:
        assert self._stop_event is not None
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


background_scheduler = ApplicationBackgroundScheduler()
