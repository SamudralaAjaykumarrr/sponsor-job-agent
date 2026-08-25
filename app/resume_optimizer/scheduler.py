"""Background resume-optimization scheduler (CLAUDE.md Phase 14 sections
56-57). Mirrors app.applications.background_scheduler's structure: a
lightweight asyncio loop owned by the FastAPI app's lifespan, doing its
actual (synchronous, DB-querying) work via asyncio.to_thread so it never
blocks the event loop or a concurrent dashboard request. Independently
gated by RESUME_OPTIMIZATION_ENABLED -- a failure in one pass must never
crash the loop, matching every other background loop in this project."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app import config
from app.db import db_session

logger = logging.getLogger("resume_optimizer.scheduler")

_IDLE_POLL_SECONDS = 15

# Jobs eligible for background optimization: analyzed/progressed far enough
# that a resume is genuinely useful, and sponsorship-eligible for review/
# application (never for NO_SPONSORSHIP/UNKNOWN jobs -- CLAUDE.md section 42
# keeps this gate untouched).
_ELIGIBLE_STATES = ("ANALYZED", "REVIEW_REQUIRED", "READY_TO_APPLY")
_ELIGIBLE_SPONSORSHIP = ("CONFIRMED_SPONSOR", "LIKELY_SPONSOR")


def _eligible_sponsorship_statuses() -> tuple:
    """CLAUDE.md production-v2 section 12 (SPONSORSHIP_POLICY): read at call
    time (not module import time) so a config change takes effect on the
    orchestrator's very next cycle without a restart, matching every other
    config-gated call site in this project."""
    if config.SPONSORSHIP_POLICY == "CONFIRMED_ONLY":
        return ("CONFIRMED_SPONSOR",)
    return _ELIGIBLE_SPONSORSHIP


def _find_jobs_needing_optimization(batch_size: int) -> list[int]:
    """A job needs (re)optimization when it has no current READY variant at
    all, or its current variant is STALE -- never when it already has a
    fresh READY one (CLAUDE.md section 58 idempotency).

    Apply/Automation Settings V1: when the Resume optimization setting is
    OFF, no AUTOMATIC caller (this background scheduler, or
    app.agent.orchestrator._run_resume_stage, which reuses this exact
    function) generates anything -- an empty candidate list, never a
    behavior change to the manual Generate/Regenerate Resume dashboard
    action or CLI, which call app.resume_optimizer.optimizer.optimize_resume
    directly and never go through this candidate query."""
    from app import apply_settings

    if apply_settings.get_settings().resume_optimization_mode == apply_settings.ResumeOptimizationMode.OFF.value:
        return []

    eligible_sponsorship = _eligible_sponsorship_statuses()
    with db_session() as conn:
        rows = conn.execute(
            f"""SELECT j.id FROM jobs j
                LEFT JOIN resume_variants rv ON rv.job_id = j.id AND rv.current = 1
                WHERE j.application_state IN ({",".join("?" for _ in _ELIGIBLE_STATES)})
                  AND j.sponsorship_status IN ({",".join("?" for _ in eligible_sponsorship)})
                  AND (rv.variant_id IS NULL OR rv.status IN ('STALE', 'GENERATING'))
                ORDER BY j.priority_score DESC
                LIMIT ?""",
            [*_ELIGIBLE_STATES, *eligible_sponsorship, batch_size],
        ).fetchall()
        return [r["id"] for r in rows]


def _run_optimization_pass() -> None:
    from app.resume_optimizer.optimizer import optimize_resume

    job_ids = _find_jobs_needing_optimization(config.RESUME_OPTIMIZATION_BATCH_SIZE)
    for job_id in job_ids:
        try:
            result = optimize_resume(job_id)
            logger.info("scheduled resume optimization: job_id=%s status=%s created=%s", job_id, result.status, result.created)
        except Exception:  # noqa: BLE001 -- one job's failure must never stop the batch
            logger.exception("scheduled resume optimization failed for job_id=%s", job_id)


class ResumeOptimizationScheduler:
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
        next_run: datetime | None = None
        while not self._stop_event.is_set():
            now = datetime.now(timezone.utc)
            if config.RESUME_OPTIMIZATION_ENABLED and (next_run is None or now >= next_run):
                try:
                    await asyncio.to_thread(_run_optimization_pass)
                except Exception:  # noqa: BLE001
                    logger.exception("resume optimization pass failed unexpectedly")
                interval = max(60, config.RESUME_OPTIMIZATION_INTERVAL_SECONDS)
                next_run = datetime.now(timezone.utc) + timedelta(seconds=interval)
            await self._wait(_IDLE_POLL_SECONDS)

    async def _wait(self, seconds: float) -> None:
        assert self._stop_event is not None
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


resume_optimization_scheduler = ResumeOptimizationScheduler()
