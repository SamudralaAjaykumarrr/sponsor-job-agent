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
actually says the moment the agent stops).

Approval-gated-autonomy-v1: AUTO_SUBMIT_ENABLED is NEVER touched by this
orchestrator, in TEST MODE or otherwise -- every stage above runs fully
automatically (discovery through form-fill/validation) and then stops at
ExecutionStatus.SUBMISSION_READY (the product-facing READY_FOR_APPROVAL
stage, see app.applications.product_state). The ONE normal human gate past
that point is an explicit APPROVE & APPLY action
(app.applications.approval.approve_and_apply) -- START AGENT, including
START AGENT (TEST MODE), must never imply approval for any job. TEST MODE
seeds a deterministic, always-safe `mock_ats` fixture job so the full
discover->...->READY_FOR_APPROVAL loop can be watched end to end without
ever touching a real employer; reaching APPLIED from there still requires
the same real approve_and_apply() call a human would make (see
`_seed_test_fixture_if_needed`, tests/test_agent_orchestrator.py, CLAUDE.md
one-click-agent section 33 as superseded by the approval-gated-autonomy-v1
spec's section 22).

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
from datetime import datetime, timedelta, timezone

from app import config
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
        # Single-orchestrator-guarantee lease (see app.agent.run_state's
        # module docstring for this section): a stable per-instance id so
        # this orchestrator can tell "still my lease" from "someone else's".
        # `_became_active` is only ever True once this instance genuinely
        # acquired the lease and started running cycles -- it gates whether
        # stop()/the loop's cleanup may touch shared actual_state/config
        # overrides at all, so a standby instance that never became active
        # can never clobber a different (real) active instance's state.
        self._instance_id = run_state_mod.new_instance_id()
        self._became_active = False

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
            # Only touch actual_state if this instance ever actually became
            # the active orchestrator -- a standby instance still waiting on
            # the lease (see _loop below) has never set RUNNING/STARTING, so
            # it must never overwrite a genuinely different (real) active
            # instance's state with STOPPING/STOPPED.
            if self._became_active:
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
        if self._became_active:
            self._restore_config_overrides()
            run_state_mod.release_orchestrator_lease(self._instance_id)
            run_state_mod.set_actual_state(AgentRunState.STOPPED)
            self._became_active = False
        return {"stopped": True}

    def status(self) -> dict:
        """CLAUDE.md production-v2 dashboard defect 1: every field the
        dashboard needs to show real in-progress status (never a misleading
        blank 'Last cycle: never' / 'Next cycle: pending' while genuinely
        RUNNING) is read directly from agent_run_state -- the orchestrator's
        own durable state -- never from app.agent.state (the separate, older
        legacy scheduler's bookkeeping, which this orchestrator never writes
        to)."""
        run_state = run_state_mod.get_run_state()
        latest = run_state_mod.latest_cycle()
        totals_24h = run_state_mod.totals_since(hours=24)
        heartbeat_age = run_state_mod.heartbeat_age_seconds()
        cycle_in_progress = (
            run_state["actual_state"] == AgentRunState.RUNNING.value
            and run_state.get("last_cycle_started_at")
            and (
                not run_state.get("last_cycle_finished_at")
                or run_state["last_cycle_finished_at"] < run_state["last_cycle_started_at"]
            )
        )
        return {
            "desired_state": run_state["desired_state"],
            "actual_state": run_state["actual_state"],
            "test_mode": run_state["test_mode"],
            "last_error": run_state["last_error"],
            "started_at": run_state["started_at"],
            "stopped_at": run_state["stopped_at"],
            "run_id": run_state.get("run_id") or "",
            "cycle_number": run_state.get("cycle_number") or 0,
            "last_cycle": latest,
            "last_cycle_started_at": run_state.get("last_cycle_started_at"),
            "last_cycle_finished_at": run_state.get("last_cycle_finished_at"),
            "cycle_in_progress": bool(cycle_in_progress),
            "next_cycle_at": run_state.get("next_cycle_at"),
            "heartbeat_at": run_state.get("heartbeat_at"),
            "heartbeat_age_seconds": heartbeat_age,
            "heartbeat_stale": bool(
                run_state["actual_state"] == AgentRunState.RUNNING.value
                and heartbeat_age is not None
                and heartbeat_age > config.AGENT_HEARTBEAT_STALE_SECONDS
            ),
            "current_stage": run_state.get("current_stage") or "",
            "current_job_label": run_state.get("current_job_label") or "",
            "lease_instance_id": run_state.get("instance_id") or "",
            "lease_expires_at": run_state.get("lease_expires_at"),
            "is_this_process_active": self._became_active,
            "totals_24h": totals_24h,
            "recent_cycles": run_state_mod.list_recent_cycles(limit=10),
            "recent_activity": run_state_mod.list_recent_activity(limit=20),
        }

    # --- internal loop ------------------------------------------------------

    async def _loop(self) -> None:
        assert self._stop_event is not None

        # --- single-orchestrator-guarantee lease: never run a single cycle
        # stage until this instance genuinely holds the lease. A second
        # process accidentally started against the same database stays here,
        # retrying on a bounded backoff, and never touches actual_state or
        # config overrides -- see app.agent.run_state's module docstring and
        # AgentOrchestrator.__init__'s _became_active comment. Self-healing:
        # if the real holder crashes without releasing, its lease simply
        # expires and this loop claims it on a later retry -- no
        # heartbeat-based liveness check involved.
        while not run_state_mod.try_acquire_orchestrator_lease(
            self._instance_id, config.AGENT_ORCHESTRATOR_LEASE_SECONDS
        ):
            run_state_mod.log_activity(
                "orchestrator_standby",
                f"instance={self._instance_id} waiting for orchestrator lease "
                "(another process already holds it)",
            )
            if await self._wait_or_stopped(min(30, max(5, config.AGENT_ORCHESTRATOR_LEASE_SECONDS // 2))):
                return  # stop requested while never active -- nothing of ours to clean up

        self._became_active = True
        lost_lease = False
        run_state_mod.set_actual_state(AgentRunState.STARTING)
        run_id = run_state_mod.new_run_id()
        run_state_mod.begin_run(run_id)
        self._apply_config_overrides()
        run_state_mod.set_actual_state(AgentRunState.RUNNING)
        test_mode_at_start = run_state_mod.is_test_mode()
        run_state_mod.log_activity(
            "agent_started", f"run_id={run_id} test_mode={test_mode_at_start} instance={self._instance_id}"
        )

        while self._should_keep_running():
            if not run_state_mod.renew_orchestrator_lease(self._instance_id, config.AGENT_ORCHESTRATOR_LEASE_SECONDS):
                # Lost the lease (should not happen under normal operation --
                # only if this process stalled past the lease window and
                # another instance reclaimed it). Never keep running cycle
                # stages without a valid lease; log and stop touching shared
                # state, matching the same defensive posture as a standby
                # instance that never acquired it in the first place.
                logger.warning("orchestrator instance %s lost its lease -- stopping", self._instance_id)
                run_state_mod.log_activity("orchestrator_lease_lost", f"instance={self._instance_id}")
                lost_lease = True
                break
            started = datetime.now(timezone.utc)
            test_mode = run_state_mod.is_test_mode()
            cycle_number = run_state_mod.mark_cycle_start(started.isoformat())
            run_state_mod.log_activity("cycle_started", f"cycle #{cycle_number}")
            try:
                counters = await asyncio.to_thread(self._run_cycle_sync, started.isoformat(), test_mode)
            except Exception as exc:  # noqa: BLE001 -- one crashed cycle must never kill the loop or the app
                logger.exception("orchestrator cycle crashed unexpectedly")
                counters = CycleCounters(errors=1, detail={"crash": str(exc)})
                run_state_mod.set_actual_state(AgentRunState.ERROR, last_error=str(exc)[:500])
                run_state_mod.log_activity("error", f"cycle #{cycle_number} crashed: {exc}"[:500])
                run_state_mod.set_actual_state(AgentRunState.RUNNING)  # self-healing: never permanently stuck in ERROR
                run_state_mod.log_activity("recovered", f"cycle #{cycle_number}: resumed RUNNING after crash")
            finished = datetime.now(timezone.utc)
            run_state_mod.record_cycle(started.isoformat(), finished.isoformat(), test_mode=test_mode, counters=counters)

            interval_seconds = max(60, config.AGENT_INTERVAL_MINUTES * 60)
            next_at = finished + timedelta(seconds=interval_seconds)
            run_state_mod.mark_cycle_finish(finished.isoformat(), next_at.isoformat())
            run_state_mod.log_activity(
                "cycle_finished",
                f"cycle #{cycle_number}: jobs={counters.jobs_processed} resumes={counters.resumes_generated} "
                f"prepared={counters.applications_prepared} submitted={counters.applications_submitted} "
                f"needs_action={counters.needs_user_action} skipped={counters.skipped} errors={counters.errors}",
            )

            await self._wait(interval_seconds)

        self._restore_config_overrides()
        # Safe to call even if lost_lease (a no-op: its own WHERE
        # instance_id = ? guard means it can never touch a lease this
        # instance no longer holds).
        run_state_mod.release_orchestrator_lease(self._instance_id)
        if not lost_lease:
            # If we lost the lease, someone else already owns it and may be
            # concurrently transitioning agent_run_state toward
            # STARTING/RUNNING -- never overwrite that with STOPPED here.
            stopped_remotely = self._stop_event is not None and not self._stop_event.is_set()
            run_state_mod.set_actual_state(AgentRunState.STOPPED)
            if stopped_remotely:
                run_state_mod.log_activity(
                    "agent_stop_detected_remote",
                    f"instance={self._instance_id}: desired_state changed away from RUNNING "
                    "(stop requested on a different process)",
                )
            else:
                run_state_mod.log_activity("agent_stopped", f"run_id={run_id}")
        self._became_active = False

    def _should_keep_running(self) -> bool:
        """False the instant a stop is requested -- either locally (this
        process's own stop_event, set immediately by stop()) or remotely
        (desired_state flipped away from RUNNING by a different process/
        instance that has no way to signal this instance's local event).
        Checked both between cycles and periodically during the inter-cycle
        wait (_wait, below), so a remote STOP is honored within a few
        seconds rather than only at the next multi-minute cycle boundary."""
        assert self._stop_event is not None
        if self._stop_event.is_set():
            return False
        return run_state_mod.get_run_state()["desired_state"] == AgentRunState.RUNNING.value

    _REMOTE_STOP_POLL_SECONDS = 5.0

    async def _wait(self, seconds: float) -> None:
        """Bounded inter-cycle wait, woken early by either the local
        stop_event or a remote desired_state flip (polled in small
        increments -- see _should_keep_running)."""
        assert self._stop_event is not None
        remaining = seconds
        while remaining > 0:
            chunk = min(self._REMOTE_STOP_POLL_SECONDS, remaining)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=chunk)
                return
            except asyncio.TimeoutError:
                pass
            remaining -= chunk
            if self._became_active and not self._should_keep_running():
                return

    async def _wait_or_stopped(self, seconds: float) -> bool:
        """Same bounded wait as _wait, but reports whether a stop was
        requested during it -- used by the lease-acquisition standby loop to
        distinguish "still waiting for the lease" from "give up, STOP was
        requested". A standby instance never became active, so it only ever
        checks the local stop_event, never desired_state (checking that here
        too would make it exit its retry loop the instant ANY stop is
        requested, even one meant for the current active instance)."""
        assert self._stop_event is not None
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        return self._stop_event.is_set()

    # --- config override management -----------------------------------------

    def _apply_config_overrides(self) -> None:
        """CLAUDE.md one-click-agent section 48: 'AUTO_PREPARE_ENABLED=true
        only while agent RUNNING if designed safely'. Snapshots the
        operator's actual `.env`-configured values first, so stopping the
        agent always restores exactly what was there before -- never
        silently clobbers an operator who also runs a standalone worker
        fleet with these flags deliberately set.

        Approval-gated-autonomy-v1: AUTO_SUBMIT_ENABLED is never raised
        here, including in TEST MODE -- the orchestrator prepares every
        eligible job (real or the TEST MODE fixture) all the way to
        READY_FOR_APPROVAL and stops; only an explicit APPROVE & APPLY
        (app.applications.approval.approve_and_apply) may unlock a
        submission attempt. See module docstring."""
        self._saved_config = {
            "APPLICATION_EXECUTOR_ENABLED": config.APPLICATION_EXECUTOR_ENABLED,
            "APPLICATION_AUTO_PREPARE_ENABLED": config.APPLICATION_AUTO_PREPARE_ENABLED,
        }
        config.APPLICATION_EXECUTOR_ENABLED = True
        config.APPLICATION_AUTO_PREPARE_ENABLED = True

    def _restore_config_overrides(self) -> None:
        for key, value in self._saved_config.items():
            setattr(config, key, value)
        self._saved_config = {}

    # --- one bounded cycle ----------------------------------------------------

    def _heartbeat(self, *, stage: str = "", job_label: str = "") -> None:
        """Every heartbeat also renews the single-orchestrator-guarantee
        lease (best-effort -- a renewal failure here is surfaced by
        app.agent.doctor's lease check and self-corrects on the loop's next
        top-of-cycle renew_orchestrator_lease call, never aborted mid-work).
        Called once per stage at minimum and once per job within the resume
        stage (see _run_resume_stage) -- this keeps the lease alive across a
        single long-running cycle (e.g. a large resume batch or a slow
        provider) rather than only renewing once per multi-stage cycle,
        which could otherwise let AGENT_ORCHESTRATOR_LEASE_SECONDS expire
        mid-cycle on an unusually large/slow one and let a standby instance
        claim it while this one is still legitimately working."""
        run_state_mod.heartbeat(stage=stage, job_label=job_label)
        if self._became_active:
            run_state_mod.renew_orchestrator_lease(self._instance_id, config.AGENT_ORCHESTRATOR_LEASE_SECONDS)

    def _run_cycle_sync(self, cycle_started_at: str, test_mode: bool) -> CycleCounters:
        counters = CycleCounters()

        self._heartbeat(stage="discovering")
        try:
            summary = run_discovery_cycle()
            counters.jobs_processed += summary.get("jobs_fetched", 0)
            counters.skipped += summary.get("hard_skips", 0)
            counters.errors += len(summary.get("errors", []))
            counters.detail["discovery"] = {
                k: summary.get(k) for k in ("jobs_new", "jobs_deduplicated", "confirmed_sponsors", "likely_sponsors")
            }
            run_state_mod.log_activity(
                "discovery_completed",
                f"found {summary.get('jobs_fetched', 0)} jobs, "
                f"{summary.get('jobs_new', 0)} new, {summary.get('hard_skips', 0)} hard-skipped",
            )
        except Exception:  # noqa: BLE001 -- one stage failing must never abort the rest of the cycle
            logger.exception("discovery stage failed")
            counters.errors += 1

        self._heartbeat(stage="generating_resumes")
        try:
            resume_stats = self._run_resume_stage()
            counters.resumes_generated += resume_stats["generated"]
            counters.one_page_success += resume_stats["one_page_success"]
            counters.one_page_overflow += resume_stats["one_page_overflow"]
            counters.one_page_compression_events += resume_stats["compression_events"]
            if resume_stats["generated"]:
                run_state_mod.log_activity(
                    "resumes_generated",
                    f"{resume_stats['generated']} generated, {resume_stats['one_page_success']} one-page ready, "
                    f"{resume_stats['one_page_overflow']} need review",
                )
        except Exception:  # noqa: BLE001
            logger.exception("resume optimization stage failed")
            counters.errors += 1

        self._heartbeat(stage="preparing_applications")
        try:
            prepared = self._run_auto_prepare_stage()
            counters.applications_prepared += prepared
            if prepared:
                run_state_mod.log_activity("applications_prepared", f"{prepared} queued for execution")
        except Exception:  # noqa: BLE001
            logger.exception("application auto-prepare stage failed")
            counters.errors += 1

        self._heartbeat(stage="executing_applications")
        try:
            exec_stats = self._run_application_worker_cycle(cycle_started_at)
            counters.applications_submitted += exec_stats["applied"]
            counters.needs_user_action += exec_stats["needs_action"]
            if exec_stats["applied"]:
                run_state_mod.log_activity("applications_applied", f"{exec_stats['applied']} confirmed applied")
            if exec_stats["needs_action"]:
                run_state_mod.log_activity("needs_user_action", f"{exec_stats['needs_action']} job(s) need your action")
        except Exception:  # noqa: BLE001
            logger.exception("application execution stage failed")
            counters.errors += 1

        self._heartbeat(stage="idle")
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
        the REVIEW_REQUIRED variant for human review.

        Apply/Automation Settings V1: a successfully one-page READY variant
        is only auto-promoted here when the persisted Auto-approve resume
        setting is ON. When it's OFF, the variant is still generated (still
        counted as a one-page success, still visible/downloadable on the
        job-detail page) but the job keeps whatever resume it had before
        until a human uses the "Approve resume" action
        (app.resume_optimizer.promotion.promote_current_variant) -- the
        exact reviewable-before-use behavior this setting's OFF state
        promises."""
        from app import apply_settings
        from app.jobs_repo import get_job
        from app.resume_optimizer.models import ResumeVariantStatus
        from app.resume_optimizer.optimizer import optimize_resume
        from app.resume_optimizer.promotion import promote_variant
        from app.resume_optimizer.repo import get_current_variant
        from app.resume_optimizer.scheduler import _find_jobs_needing_optimization

        stats = dict(generated=0, one_page_success=0, one_page_overflow=0, compression_events=0)
        job_ids = _find_jobs_needing_optimization(config.MAX_RESUMES_PER_CYCLE)
        auto_approve_resume = apply_settings.get_settings().auto_approve_resume

        for job_id in job_ids:
            # Watchdog diagnostics: heartbeat per job, not just once per
            # stage -- with MAX_RESUMES_PER_CYCLE jobs in one stage, a stage-
            # only heartbeat can't distinguish "working through a big batch"
            # from "stuck on job N" until the whole stage finishes, which can
            # be well past AGENT_HEARTBEAT_STALE_SECONDS on a large batch.
            self._heartbeat(stage="generating_resumes", job_label=f"job {job_id}")
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
                if auto_approve_resume:
                    job = get_job(job_id)
                    if job is not None:
                        promote_variant(job_id, variant)
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
            is_test_fixture=True,
        )
        ingest_and_process(job)
        logger.info("test-mode: seeded mock_ats fixture job external_job_id=%s", _TEST_FIXTURE_EXTERNAL_ID)


orchestrator = AgentOrchestrator()
