import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app import config
from app.agent import state as agent_state
from app.agent.cycle import run_discovery_cycle

logger = logging.getLogger("agent.scheduler")

_DISABLED_POLL_SECONDS = 5


class AgentScheduler:
    """Background asyncio loop. The sync discovery cycle runs in a worker
    thread (asyncio.to_thread) so it never blocks the event loop / dashboard
    requests. A module-level singleton is reused across the app's lifespan."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event = asyncio.Event()
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
            if not agent_state.is_enabled():
                agent_state.set_next_cycle_at(None)
                await self._wait(_DISABLED_POLL_SECONDS)
                continue

            agent_state.mark_cycle_start()
            try:
                summary = await asyncio.to_thread(run_discovery_cycle)
                agent_state.mark_cycle_end(summary)
            except Exception:
                logger.exception("discovery cycle failed unexpectedly")
                agent_state.mark_cycle_end({"errors": ["cycle crashed -- see server logs"]})

            interval_seconds = max(60, config.DISCOVERY_INTERVAL_MINUTES * 60)
            next_at = datetime.now(timezone.utc) + timedelta(seconds=interval_seconds)
            agent_state.set_next_cycle_at(next_at.isoformat())
            await self._wait(interval_seconds)

    async def _wait(self, seconds: float) -> None:
        assert self._stop_event is not None
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


scheduler = AgentScheduler()
