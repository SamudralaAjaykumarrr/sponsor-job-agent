"""Phase 9 standalone application-executor worker daemon (CLAUDE.md Phase 9
section 2). Reuses the entire Phase 8 executor pipeline
(app.applications.executor.process_execution) unchanged -- this module only
adds leasing (already built in Phase 8's app.applications.queue), submission
circuit-breaker bookkeeping, per-attempt history, worker identity/heartbeat/
capability declaration, drain-mode support, and graceful shutdown around it.

Run directly:
    python -m app.applications.worker run [--once] [--workers N]

Discovery workers and application workers are logically separate (CLAUDE.md
Phase 9 section 3): this worker declares ONLY APPLICATION_PREPARE/
APPLICATION_SUBMIT capabilities via app.workers.repo.upsert_worker's
`capabilities` column -- it never touches app.workers.queue/leasing (the
discovery poll/verification queues) at all, and a plain Phase 5-7 discovery
worker (which never sets `capabilities`) can never claim an application
execution, since app.applications.queue.claim_execution_batch is an entirely
separate claim path over a different table."""

import logging
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

from app import config
from app.applications import attempts as attempts_repo
from app.applications import circuit as app_circuit
from app.applications import queue as app_queue
from app.applications.executor import process_execution
from app.applications.models import ExecutionStatus
from app.applications.provider_registry import get_application_provider
from app.applications.worker_capabilities import WorkerCapability, encode as encode_capabilities
from app.db import init_db
from app.jobs_repo import get_job
from app.workers import reaper
from app.workers import repo as workers_repo
from app.workers.identity import generate_worker_identity
from app.workers.models import WorkerStatus

logger = logging.getLogger("applications.worker")

# Executions that reach one of these terminal-for-this-call statuses imply
# provider.submit() was genuinely invoked this call -- only these feed the
# submission circuit breaker. Anything else (NEEDS_USER_ACTION,
# SUBMISSION_READY, VALIDATION_REQUIRED, DUPLICATE_APPLICATION_BLOCKED,
# JOB_NO_LONGER_ACTIVE, ...) means submit() was never reached, so recording
# a "success"/"failure" against the breaker would be meaningless noise.
_SUBMIT_ATTEMPTED_STATUSES = {
    ExecutionStatus.SUBMITTED.value, ExecutionStatus.APPLIED.value,
    ExecutionStatus.SUBMISSION_CONFIRMED.value, ExecutionStatus.SUBMISSION_STATUS_UNKNOWN.value,
    ExecutionStatus.PERMANENT_SUBMISSION_FAILURE.value, ExecutionStatus.RETRYABLE_SUBMISSION_FAILURE.value,
}
_SUBMIT_SUCCESS_STATUSES = {
    ExecutionStatus.SUBMITTED.value, ExecutionStatus.APPLIED.value, ExecutionStatus.SUBMISSION_CONFIRMED.value,
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class ApplicationWorker:
    """One application-executor worker process's execution loop. Safe to run
    many of these concurrently (local threads/processes, or -- with a shared
    Postgres DB -- separate machines) against the same database, exactly
    like app.workers.runner.Worker for discovery."""

    def __init__(
        self, *, single_cycle: bool = False, idle_sleep_seconds: Optional[float] = None,
        start_draining: bool = False,
    ) -> None:
        self.identity = generate_worker_identity()
        self.single_cycle = single_cycle
        self.idle_sleep_seconds = (
            config.APPLICATION_WORKER_IDLE_SLEEP_SECONDS if idle_sleep_seconds is None else idle_sleep_seconds
        )
        self._stop = threading.Event()
        self._start_draining = start_draining
        self.executions_processed = 0
        self.submissions_attempted = 0
        self.errors = 0

    # --- lifecycle -----------------------------------------------------

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame) -> None:
        logger.info("application worker %s received signal %s -- stopping after in-flight work completes",
                    self.identity.worker_id, signum)
        self.request_stop()

    def request_stop(self) -> None:
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def _is_draining(self) -> bool:
        row = workers_repo.get_worker(self.identity.worker_id)
        return bool(row) and row["status"] == WorkerStatus.DRAINING.value

    def run(self) -> None:
        if not config.APPLICATION_EXECUTOR_ENABLED:
            raise RuntimeError(
                "APPLICATION_EXECUTOR_ENABLED is false -- an application worker must not be started "
                "while the executor is disabled."
            )
        init_db()
        capabilities = encode_capabilities([WorkerCapability.APPLICATION_PREPARE, WorkerCapability.APPLICATION_SUBMIT])
        initial_status = WorkerStatus.DRAINING.value if self._start_draining else WorkerStatus.STARTING.value
        workers_repo.upsert_worker(
            self.identity.worker_id, hostname=self.identity.hostname, pid=self.identity.pid,
            shard_index=0, shard_count=1, status=initial_status,
            worker_version=self.identity.worker_version, schema_version=self.identity.schema_version,
            capability_version=self.identity.capability_version, backend=self.identity.backend,
            capabilities=capabilities,
        )
        logger.info("application worker %s starting (backend=%s)", self.identity.worker_id, self.identity.backend)
        try:
            while True:
                self._run_cycle()
                reaper.reap_orphans(stale_after_seconds=config.ORPHAN_WORKER_STALE_SECONDS)
                if self._stop.is_set() or self.single_cycle:
                    break
                if not self._is_draining():
                    self._heartbeat(WorkerStatus.IDLE)
                self._interruptible_sleep(self.idle_sleep_seconds)
                if self._stop.is_set():
                    break
        finally:
            self._heartbeat(WorkerStatus.STOPPED)
            logger.info(
                "application worker %s stopped (executions_processed=%s submissions_attempted=%s errors=%s)",
                self.identity.worker_id, self.executions_processed, self.submissions_attempted, self.errors,
            )

    def _interruptible_sleep(self, seconds: float) -> None:
        self._stop.wait(timeout=seconds)

    def _heartbeat(self, status: WorkerStatus) -> None:
        workers_repo.heartbeat_worker(
            self.identity.worker_id, status=status.value,
            portals_processed=self.executions_processed, jobs_processed=self.submissions_attempted,
            errors=self.errors,
        )

    # --- one bounded cycle -----------------------------------------------

    def _run_cycle(self) -> dict:
        cycle_start = time.monotonic()
        stats = dict(claimed=0, applied=0, needs_action=0, submitted=0, failed=0, deferred_draining=0)

        draining = self._is_draining()
        self._heartbeat(WorkerStatus.DRAINING if draining else WorkerStatus.WORKING)
        if draining:
            # CLAUDE.md Phase 9 section 13: a draining worker never claims
            # new executions -- it still heartbeats (above) so it remains
            # visible/known-alive to operators until explicitly stopped.
            return stats

        budget = config.APPLICATION_WORKER_CYCLE_TIME_BUDGET_SECONDS
        max_items = config.APPLICATION_MAX_EXECUTIONS_PER_WORKER_CYCLE
        processed = 0

        with ThreadPoolExecutor(max_workers=max(1, config.APPLICATION_WORKER_CONCURRENCY)) as pool:
            while not self._stop.is_set():
                if self._is_draining():
                    break
                elapsed = time.monotonic() - cycle_start
                if elapsed >= budget or processed >= max_items:
                    break
                batch_limit = max(1, min(max_items - processed, 5))
                items = app_queue.claim_execution_batch(
                    worker_id=self.identity.worker_id, limit=batch_limit,
                    lease_seconds=config.APPLICATION_LEASE_SECONDS,
                )
                if not items:
                    break
                stats["claimed"] += len(items)
                futures = {pool.submit(self._process_claimed, item, stats): item for item in items}
                for future in as_completed(futures):
                    item = futures[future]
                    try:
                        future.result()
                    except Exception:
                        logger.exception("unhandled error processing execution %s", item.get("execution_id"))
                        self.errors += 1
                    processed += 1
                    self.executions_processed += 1
                if processed >= max_items:
                    break

        return stats

    # --- one claimed execution --------------------------------------------

    def _cooldown_skip(self, item: dict) -> None:
        """Mirrors app.workers.runner._cooldown_skip: an item cancelled
        before ever being attempted (circuit open / provider at its
        concurrency limit) gets a short lease EXTENSION, never a bare
        release -- avoids a busy-spin of claim/cancel/reclaim across
        multiple application workers sharing one provider's tight
        submission concurrency budget (CLAUDE.md Phase 9 section 5's own
        duplicate-safety concern extends to this too: releasing outright
        would let a second worker claim the SAME execution moments later)."""
        ok = app_queue.extend_execution_lease(
            item["execution_id"], item["lease_attempt_id"], lease_seconds=config.APPLICATION_SKIP_COOLDOWN_SECONDS,
        )
        if not ok:
            app_queue.release_execution_lease(item["execution_id"], expected_attempt_id=item["lease_attempt_id"])

    def _process_claimed(self, item: dict, stats: dict) -> None:
        execution_id = item["execution_id"]
        job_id = item["job_id"]
        job = get_job(job_id)
        provider_name = get_application_provider(job).name if job is not None else (item.get("provider") or "unknown")

        if not app_circuit.may_attempt(provider_name):
            attempt = attempts_repo.ApplicationAttemptRecord(
                attempt_id=attempts_repo.new_attempt_id(), execution_id=execution_id, job_id=job_id,
                worker_id=self.identity.worker_id, provider=provider_name, stage="claimed",
                result="CANCELLED", error_type="circuit_open", finished_at=utcnow(),
                correlation_id=item.get("correlation_id") or "",
            )
            attempts_repo.record_attempt(attempt)
            self._cooldown_skip(item)
            return

        if not app_circuit.acquire_inflight_slot(provider_name):
            app_circuit.release_half_open_probe(provider_name)
            attempt = attempts_repo.ApplicationAttemptRecord(
                attempt_id=attempts_repo.new_attempt_id(), execution_id=execution_id, job_id=job_id,
                worker_id=self.identity.worker_id, provider=provider_name, stage="claimed",
                result="CANCELLED", error_type="provider_concurrency_limit", finished_at=utcnow(),
                correlation_id=item.get("correlation_id") or "",
            )
            attempts_repo.record_attempt(attempt)
            self._cooldown_skip(item)
            return

        attempt_id = attempts_repo.new_attempt_id()
        started_at = utcnow()
        try:
            self._execute_claimed(item, provider_name, attempt_id, started_at, stats)
        finally:
            app_circuit.release_inflight_slot(provider_name)

    def _execute_claimed(self, item: dict, provider_name: str, attempt_id: str, started_at: str, stats: dict) -> None:
        execution_id = item["execution_id"]
        job_id = item["job_id"]
        correlation_id = item.get("correlation_id") or ""
        draining_now = self._is_draining()

        try:
            execution = process_execution(execution_id, allow_submission=not draining_now)
        except Exception as exc:  # noqa: BLE001 -- final safety net: an attempt must ALWAYS be
            # recorded and the lease ALWAYS released, even for a failure mode
            # process_execution()'s own handling didn't anticipate. Releasing
            # (rather than extending) is safe regardless of how far
            # process_execution() got, because process_execution() itself
            # now treats a resumed SUBMITTING/SUBMITTED row as
            # SUBMISSION_STATUS_UNKNOWN rather than ever re-calling submit().
            logger.exception("unhandled error executing application %s", execution_id)
            attempts_repo.record_attempt(attempts_repo.ApplicationAttemptRecord(
                attempt_id=attempt_id, execution_id=execution_id, job_id=job_id, worker_id=self.identity.worker_id,
                provider=provider_name, started_at=started_at, finished_at=utcnow(), stage="execute",
                result="WORKER_EXCEPTION", retryable=True, error_type=type(exc).__name__,
                safe_error_message=str(exc)[:300], correlation_id=correlation_id,
            ))
            self.errors += 1
            app_queue.release_execution_lease(execution_id, expected_attempt_id=item["lease_attempt_id"])
            return

        status = execution["status"]
        finished_at = utcnow()

        if status in _SUBMIT_ATTEMPTED_STATUSES:
            self.submissions_attempted += 1
            stats["submitted"] += 1
            success = status in _SUBMIT_SUCCESS_STATUSES
            app_circuit.record_result(provider_name, success=success)
        else:
            app_circuit.release_half_open_probe(provider_name)

        if status == ExecutionStatus.APPLIED.value:
            stats["applied"] += 1
        elif status in (ExecutionStatus.NEEDS_USER_ACTION.value, ExecutionStatus.VALIDATION_REQUIRED.value,
                        ExecutionStatus.SUBMISSION_STATUS_UNKNOWN.value):
            stats["needs_action"] += 1
        elif status in (ExecutionStatus.PERMANENT_SUBMISSION_FAILURE.value, ExecutionStatus.SUBMISSION_FAILED.value,
                        ExecutionStatus.JOB_NO_LONGER_ACTIVE.value, ExecutionStatus.DUPLICATE_APPLICATION_BLOCKED.value):
            stats["failed"] += 1

        attempts_repo.record_attempt(attempts_repo.ApplicationAttemptRecord(
            attempt_id=attempt_id, execution_id=execution_id, job_id=job_id, worker_id=self.identity.worker_id,
            provider=provider_name, started_at=started_at, finished_at=finished_at, stage=status,
            result=status, retryable=status in (ExecutionStatus.RETRYABLE_SUBMISSION_FAILURE.value,),
            confirmation_observed=bool(execution.get("confirmation_id")),
            error_type=execution.get("error_type") or "", safe_error_message=(execution.get("error_message_safe") or "")[:500],
            correlation_id=correlation_id,
        ))

        # Always release -- whatever status process_execution() left this
        # execution in, it will never again match claim_execution_batch's
        # claimable-status set unless it's a genuinely still-in-flight status
        # (which won't happen from a normal, non-crashing return), so leaving
        # the lease held would only delay dashboard visibility for no benefit.
        app_queue.release_execution_lease(execution_id, expected_attempt_id=item["lease_attempt_id"])


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m app.applications.worker")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run one or more application-executor worker processes")
    p_run.add_argument("--once", action="store_true", help="run exactly one bounded cycle then exit")
    p_run.add_argument("--workers", type=int, default=1, help="spawn N worker processes via the supervisor")
    p_run.add_argument("--drain", action="store_true", help="start immediately in DRAINING status")

    args = parser.parse_args(argv)
    if args.command == "run":
        if args.workers > 1:
            from app.applications.supervisor import ApplicationSupervisor

            supervisor = ApplicationSupervisor(args.workers)
            supervisor.run_until_interrupted()
            return 0

        if config.STRUCTURED_LOGGING_ENABLED:
            from app.observability.logging_config import configure_structured_logging

            configure_structured_logging()

        worker = ApplicationWorker(single_cycle=args.once, start_draining=args.drain)
        worker.install_signal_handlers()
        worker.run()
        return 0
    return 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
