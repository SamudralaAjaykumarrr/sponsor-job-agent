"""One-click autonomous agent orchestrator (see docs/one-click-agent.md,
docs/autonomous-orchestration.md). This is the single service the dashboard's
START AGENT / STOP AGENT button drives -- it coordinates, in order, every
existing stage this project already built as separate opt-in pieces:

    discovery (app.agent.cycle.run_discovery_cycle, unchanged)
    -> resume optimization + one-page enforcement (app.resume_optimizer)
    -> application auto-prepare (app.applications.scheduler.run_cycle)
    -> application execution (app.applications.worker.ApplicationWorker)

It deliberately does NOT reimplement any of those stages -- it only decides
WHEN they run and, for the two stages that are independently gated by a
static (`.env`-only) config flag, temporarily raises that flag for the
duration the agent is RUNNING (restored to whatever the operator's `.env`
actually says the moment the agent stops). AUTO_SUBMIT_ENABLED is
deliberately NEVER touched by a normal run -- it stays whatever `.env` says
(default False), so real submission only ever happens through the existing,
unchanged AUTO_PERMITTED/provider-capability gates in
app.applications.executor. TEST MODE is the one exception: it seeds a
deterministic, always-safe `mock_ats` fixture job and temporarily allows
AUTO_SUBMIT_ENABLED so the full discover->...->APPLIED loop can be watched
end to end without ever touching a real employer -- see
`_seed_test_fixture_if_needed`/CLAUDE.md one-click-agent section 33.

Hard gates this module must never weaken (CLAUDE.md, reaffirmed by the
one-click-agent build brief section 3): FULL_TIME-only, sponsorship gate,
job identity, claim checker, resume freshness, duplicate protection,
application budgets, provider capability truth, CAPTCHA/MFA/login/legal
boundaries. None of those live in this file -- they live in
app.pipeline/app.applications.eligibility/app.applications.executor exactly
as before, and this module calls into them, never around them."""

import asyncio
import json
import logging
from datetime import datetime, timezone

from app import config
from app.agent import state as agent_state
from app.agent.run_state import AgentRunState, CycleCounters
from app.agent import run_state as run_state_mod
from app.agent.cycle import run_discovery_cycle

logger = logging.getLogger("agent.orchestrator")

_TEST_FIXTURE_EXTERNAL_ID = "agent-test-mode-fixture-1"


class AgentOrchestrator:
    """Module-level singleton, mirroring app.agent.scheduler.AgentScheduler /
    app.applications.background_scheduler.ApplicationBackgroundScheduler's
    structure exactly: a lightweight asyncio loop owned by the FastAPI app's
    lifespan, with the actual synchronous DB/network work run via
    asyncio.to_thread so it never blocks the event loop or a concurrent
    dashboard request."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._application_worker = None  # lazily created; reused across cycles (one worker identity, not one per cycle)
        self._saved_config: dict = {}

    # --- public control surface ------------------------------------------

    def start(self, *, test_mode: bool = False) -> dict:
        current = run_state_mod.get_run_state()
        already = current["actual_state"] in (AgentRunState.RUNNING.value, AgentRunState.STARTING.value)
        if already and bool(current["test_mode"]) == test_mode:
            return {"started": False, "reason": "already running"}

        run_state_mod.set_desired_state(AgentRunState.RUNNING, test_mode=test_mode)
        if test_mode:
            try:
                self._seed_test_fixture_if_needed()
            except Exception:  # noqa: BLE001 -- seeding failure must never block starting the agent
                logger.exception("test-mode fixture seeding failed")

        if not self._task or self._task.done():
            self._stop_event = asyncio.Event()
            self._task = asyncio.create_task(self._loop())
        return {"started": True}

    async def stop(self) -> dict:
        run_state_mod.set_desired_state(AgentRunState.STOPPED)
        if self._task and not self._task.done():
            run_state_mod.set_actual_state(AgentRunState.STOPPING)
            assert self._stop_event is not None
            self._stop_event.set()
            try:
                # Bounded wait for the in-flight cycle to reach a safe
                # stopping point (CLAUDE.md: "no abrupt interruption of
                # possible irreversible submit state") -- a cycle already
                # mid-submission is additionally protected by the executor's
                # own SUBMITTING/SUBMITTED crash-resume guard regardless.
                await asyncio.wait_for(self._task, timeout=90)
            except asyncio.TimeoutError:
                logger.warning("orchestrator did not stop within the grace period -- cancelling")
                self._task.cancel()
        agent_state.set_enabled(False)
        self._restore_config_overrides()
        run_state_mod.set_actual_state(AgentRunState.STOPPED)
        return {"stopped": True}

    def status(self) -> dict:
        run_state = run_state_mod.get_run_state()
        latest = run_state_mod.latest_cycle()
        totals_24h = run_state_mod.totals_since(hours=24)
        return {
            "desired_state": run_state["desired_state"],
            "actual_state": run_state["actual_state"],
            "test_mode": run_state["test_mode"],
            "last_error": run_state["last_error"],
            "started_at": run_state["started_at"],
            "stopped_at": run_state["stopped_at"],
            "last_cycle": latest,
            "next_cycle_at": agent_state.get_status().get("next_cycle_at"),
            "totals_24h": totals_24h,
            "recent_cycles": run_state_mod.list_recent_cycles(limit=10),
        }

    # --- internal loop ------------------------------------------------------

    async def _loop(self) -> None:
        assert self._stop_event is not None
        run_state_mod.set_actual_state(AgentRunState.STARTING)
        self._apply_config_overrides()
        agent_state.set_enabled(True)
        run_state_mod.set_actual_state(AgentRunState.RUNNING)

        while not self._stop_event.is_set():
            started = datetime.now(timezone.utc)
            test_mode = run_state_mod.is_test_mode()
            try:
                counters = await asyncio.to_thread(self._run_cycle_sync, started.isoformat(), test_mode)
            except Exception as exc:  # noqa: BLE001 -- one crashed cycle must never kill the loop or the app
                logger.exception("orchestrator cycle crashed unexpectedly")
                counters = CycleCounters(errors=1, detail={"crash": str(exc)})
                run_state_mod.set_actual_state(AgentRunState.ERROR, last_error=str(exc)[:500])
                run_state_mod.set_actual_state(AgentRunState.RUNNING)  # self-healing: never permanently stuck in ERROR
            finished = datetime.now(timezone.utc)
            run_state_mod.record_cycle(started.isoformat(), finished.isoformat(), test_mode=test_mode, counters=counters)

            interval_seconds = max(60, config.AGENT_INTERVAL_MINUTES * 60)
            await self._wait(interval_seconds)

        agent_state.set_enabled(False)
        self._restore_config_overrides()
        run_state_mod.set_actual_state(AgentRunState.STOPPED)

    async def _wait(self, seconds: float) -> None:
        assert self._stop_event is not None
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # --- config override management -----------------------------------------

    def _apply_config_overrides(self) -> None:
        """CLAUDE.md one-click-agent section 48: 'AUTO_PREPARE_ENABLED=true
        only while agent RUNNING if designed safely'. Snapshots the
        operator's actual `.env`-configured values first, so stopping the
        agent always restores exactly what was there before -- never
        silently clobbers an operator who also runs a standalone worker
        fleet with these flags deliberately set. AUTO_SUBMIT_ENABLED is only
        ever touched in TEST MODE (mock_ats only, see module docstring)."""
        self._saved_config = {
            "APPLICATION_EXECUTOR_ENABLED": config.APPLICATION_EXECUTOR_ENABLED,
            "APPLICATION_AUTO_PREPARE_ENABLED": config.APPLICATION_AUTO_PREPARE_ENABLED,
        }
        config.APPLICATION_EXECUTOR_ENABLED = True
        config.APPLICATION_AUTO_PREPARE_ENABLED = True
        if run_state_mod.is_test_mode():
            self._saved_config["AUTO_SUBMIT_ENABLED"] = config.AUTO_SUBMIT_ENABLED
            config.AUTO_SUBMIT_ENABLED = True

    def _restore_config_overrides(self) -> None:
        for key, value in self._saved_config.items():
            setattr(config, key, value)
        self._saved_config = {}

    # --- one bounded cycle ----------------------------------------------------

    def _run_cycle_sync(self, cycle_started_at: str, test_mode: bool) -> CycleCounters:
        counters = CycleCounters()

        try:
            summary = run_discovery_cycle()
            counters.jobs_processed += summary.get("jobs_fetched", 0)
            counters.skipped += summary.get("hard_skips", 0)
            counters.errors += len(summary.get("errors", []))
            counters.detail["discovery"] = {
                k: summary.get(k) for k in ("jobs_new", "jobs_deduplicated", "confirmed_sponsors", "likely_sponsors")
            }
        except Exception:  # noqa: BLE001 -- one stage failing must never abort the rest of the cycle
            logger.exception("discovery stage failed")
            counters.errors += 1

        try:
            resume_stats = self._run_resume_stage()
            counters.resumes_generated += resume_stats["generated"]
            counters.one_page_success += resume_stats["one_page_success"]
            counters.one_page_overflow += resume_stats["one_page_overflow"]
            counters.one_page_compression_events += resume_stats["compression_events"]
        except Exception:  # noqa: BLE001
            logger.exception("resume optimization stage failed")
            counters.errors += 1

        try:
            prepared = self._run_auto_prepare_stage()
            counters.applications_prepared += prepared
        except Exception:  # noqa: BLE001
            logger.exception("application auto-prepare stage failed")
            counters.errors += 1

        try:
            exec_stats = self._run_application_worker_cycle(cycle_started_at)
            counters.applications_submitted += exec_stats["applied"]
            counters.needs_user_action += exec_stats["needs_action"]
        except Exception:  # noqa: BLE001
            logger.exception("application execution stage failed")
            counters.errors += 1

        return counters

    def _run_resume_stage(self) -> dict:
        """Generates/refreshes the JD-tailored, one-page-enforced resume for
        every eligible job that doesn't already have a current READY variant
        (app.resume_optimizer.scheduler's own candidate query, reused
        unchanged), then PROMOTES a successfully one-page READY variant onto
        the job row (jobs.resume_docx_path/pdf_path/txt_path) so it becomes
        the actual artifact the application executor uploads -- CLAUDE.md
        one-click-agent section 9/27 'link exact resume variant' /
        'verify resume artifact hash before upload'. A variant that could
        not safely reach one page (REVIEW_REQUIRED) is never promoted -- the
        job keeps whatever resume it had before, and the dashboard surfaces
        the REVIEW_REQUIRED variant for human review."""
        from app.jobs_repo import get_job, update_job
        from app.resume_optimizer.models import ResumeVariantStatus
        from app.resume_optimizer.optimizer import optimize_resume
        from app.resume_optimizer.repo import get_current_variant
        from app.resume_optimizer.scheduler import _find_jobs_needing_optimization

        stats = dict(generated=0, one_page_success=0, one_page_overflow=0, compression_events=0)
        job_ids = _find_jobs_needing_optimization(config.MAX_RESUMES_PER_CYCLE)

        for job_id in job_ids:
            try:
                result = optimize_resume(job_id)
            except Exception:  # noqa: BLE001 -- one job's optimization failure must never stop the batch
                logger.exception("resume optimization failed for job_id=%s", job_id)
                continue
            if not result.created:
                continue
            stats["generated"] += 1

            variant = get_current_variant(job_id)
            if variant is None:
                continue
            stats["compression_events"] += variant.get("compression_steps_applied") or 0

            if variant["status"] == ResumeVariantStatus.READY.value and variant.get("page_count") == 1:
                stats["one_page_success"] += 1
                job = get_job(job_id)
                if job is not None:
                    update_job(
                        job_id,
                        resume_docx_path=variant["resume_docx_path"], resume_pdf_path=variant["resume_pdf_path"],
                        resume_txt_path=variant["resume_txt_path"], resume_jd_fingerprint=variant["jd_fingerprint"],
                        promoted_resume_variant_id=variant["variant_id"],
                    )
            elif variant["status"] == ResumeVariantStatus.REVIEW_REQUIRED.value:
                stats["one_page_overflow"] += 1

        return stats

    def _run_auto_prepare_stage(self) -> int:
        from app.applications import scheduler as applications_scheduler

        result = applications_scheduler.run_cycle(limit=config.MAX_APPLICATIONS_PER_CYCLE)
        return result.queued

    def _run_application_worker_cycle(self, cycle_started_at: str) -> dict:
        """Reuses app.applications.worker.ApplicationWorker's fully public,
        tested `run(single_cycle=True)` entrypoint -- the exact mechanism
        `python -m app.applications.worker run --once` already exposes for
        'run one bounded pass and exit' -- rather than a second, parallel
        execution loop. One worker identity is created lazily and reused for
        the orchestrator's whole lifetime (CLAUDE.md: 'do not launch
        duplicate workers'), not a fresh one per cycle. Per-cycle
        applied/needs-action counts are read back from the database (this
        project's existing 'live query over persisted state' metrics
        convention) rather than threaded through ApplicationWorker's return
        value, since `run()` intentionally returns nothing (its
        instance counters accumulate for the worker's whole lifetime, not
        per-cycle)."""
        if not config.APPLICATION_EXECUTOR_ENABLED:
            return {"applied": 0, "needs_action": 0}

        from app.applications.worker import ApplicationWorker
        from app.db import db_session

        if self._application_worker is None:
            self._application_worker = ApplicationWorker(single_cycle=True)
        self._application_worker.run()

        with db_session() as conn:
            applied = conn.execute(
                "SELECT COUNT(*) AS c FROM application_executions WHERE status = 'APPLIED' AND updated_at >= ?",
                (cycle_started_at,),
            ).fetchone()["c"]
            needs_action = conn.execute(
                "SELECT COUNT(*) AS c FROM application_executions WHERE requires_user_action = 1 AND updated_at >= ?",
                (cycle_started_at,),
            ).fetchone()["c"]
        return {"applied": applied, "needs_action": needs_action}

    # --- test mode ------------------------------------------------------------

    def _seed_test_fixture_if_needed(self) -> None:
        """CLAUDE.md one-click-agent section 33/35: a deterministic,
        always-safe `mock_ats` fixture job so TEST MODE can demonstrate the
        full discover->...->APPLIED loop without ever touching a real
        employer. Idempotent -- re-seeds nothing if the fixture already
        exists (matched by its fixed external_job_id, mirroring every other
        provider's own stable-ID dedup)."""
        from app.jobs_repo import get_job_by_provider_external_id
        from app.models import ApplicationMode, Job
        from app.pipeline import ingest_and_process

        existing = get_job_by_provider_external_id("mock_ats", _TEST_FIXTURE_EXTERNAL_ID)
        if existing is not None:
            return

        job = Job(
            title="Backend Software Engineer",
            company="Test Fixture Co",
            location="Remote - US",
            description=(
                "We are hiring a Backend Software Engineer to build REST APIs in Python using FastAPI, "
                "with PostgreSQL, Docker, and CI/CD pipelines. This is a full-time position. "
                "H-1B sponsorship is available for this role."
            ),
            employment_type="Full-time",
            provider="mock_ats",
            external_job_id=_TEST_FIXTURE_EXTERNAL_ID,
            url=f"https://mock-ats.local/jobs/{_TEST_FIXTURE_EXTERNAL_ID}",
            provider_metadata=json.dumps({"mock_scenario": "simple"}),
            mode=ApplicationMode.ASSIST,
        )
        ingest_and_process(job)
        logger.info("test-mode: seeded mock_ats fixture job external_job_id=%s", _TEST_FIXTURE_EXTERNAL_ID)


orchestrator = AgentOrchestrator()
