"""The Phase 5 worker: claim -> lease -> discover/verify -> normalize ->
dedupe -> store -> analyze -> release -> reschedule, repeated until stopped.
Reuses the existing Phase 2/3 discovery pipeline (app.agent.cycle) and Phase
4 verification/lifecycle pipeline (app.registry.verification/lifecycle/sync)
entirely -- this module only adds leasing, retry/circuit-breaker/dead-letter
bookkeeping, attempt history, worker identity/heartbeat, sharding, and
graceful shutdown around them. See docs/worker-architecture.md."""

import logging
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

import httpx

from app import config, migrations
from app.agent.cycle import process_raw_job
from app.db import init_db
from app.jobs_repo import finalize_discovery_cycle, insert_discovery_log, start_discovery_cycle
from app.providers.registry import build_provider_for_tenant
from app.registry import lifecycle as registry_lifecycle
from app.registry import repo as registry_repo
from app.registry import store as registry_store
from app.registry import sync as registry_sync
from app.registry import probe as probe_mod
from app.registry.models import VerificationResult
from app.registry.verification import verify_portal
from app.workers import circuit, dead_letter, reaper, retry, schema_check
from app.workers import schema_drift_repo
from app.workers import repo as workers_repo
from app.workers.identity import generate_worker_identity
from app.workers.models import AttemptRecord, AttemptStatus, LeasedWorkItem, PortalType, WorkerStatus
from app.workers.queue import SQLitePollQueue, SQLiteVerificationQueue

logger = logging.getLogger("workers.runner")

_TERMINAL_VERIFICATION_STATES = {"VERIFIED", "ACTIVE", "QUARANTINED"}

# When a claimed item is skipped this cycle purely because the circuit is
# open or the provider is at its concurrency limit, the lease is extended by
# a short cooldown rather than released immediately. Releasing immediately
# would make the row instantly reclaimable, and with several workers/threads
# all sharing one provider's small concurrency budget that becomes a busy
# spin (claim -> cancel -> reclaim -> cancel -> ...) that wastes claim
# round-trips and floods attempt history without doing any real work. A
# short cooldown still lets the row be tried again well within one normal
# cycle, it just isn't hammered continuously.
_SKIP_COOLDOWN_SECONDS = 5


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Worker:
    """One worker process's execution loop. Safe to run many of these
    concurrently (local threads, local processes, or -- with a shared DB --
    separate machines in the future) against the same database."""

    def __init__(
        self, *, shard_index: Optional[int] = None, shard_count: Optional[int] = None,
        single_cycle: bool = False, idle_sleep_seconds: float = 5.0,
    ) -> None:
        self.identity = generate_worker_identity()
        self.shard_index = config.REGISTRY_SHARD_INDEX if shard_index is None else shard_index
        self.shard_count = config.REGISTRY_SHARD_COUNT if shard_count is None else shard_count
        self.poll_queue = SQLitePollQueue()
        self.verify_queue = SQLiteVerificationQueue()
        self.single_cycle = single_cycle
        self.idle_sleep_seconds = idle_sleep_seconds
        self._stop = threading.Event()
        self.portals_processed = 0
        self.jobs_processed = 0
        self.errors = 0
        self._signals_installed = False

    # --- lifecycle -----------------------------------------------------

    def install_signal_handlers(self) -> None:
        """Only safe to call from the main thread of the main interpreter --
        the CLI entrypoint does this; tests that drive Worker directly in a
        background thread must not call it (Python forbids non-main-thread
        signal handlers)."""
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        self._signals_installed = True

    def _handle_signal(self, signum, frame) -> None:
        logger.info("worker %s received signal %s -- stopping after in-flight work completes", self.identity.worker_id, signum)
        self.request_stop()

    def request_stop(self) -> None:
        self._stop.set()

    def _check_schema_compatibility(self) -> None:
        """CLAUDE.md Phase 6 section 19: 'Reject or warn when a worker is
        incompatible with DB schema. Do not silently corrupt state.' A
        worker whose code expects migrations the live database hasn't
        applied yet would immediately hit real 'column does not exist'
        errors on its very first query -- refuse to start instead. A worker
        whose code is OLDER than what's recorded (a mixed-version rollout,
        some workers upgraded already) is allowed to proceed with a warning:
        every Phase 6 migration is purely additive, so an older worker
        simply not using new columns/tables is safe, not corrupting."""
        from app.db import db_session

        with db_session() as conn:
            db_version = migrations.current_db_version(conn)
        if db_version < migrations.CURRENT_SCHEMA_VERSION:
            raise RuntimeError(
                f"worker schema_version={migrations.CURRENT_SCHEMA_VERSION} but the database is only at "
                f"schema_version={db_version} -- refusing to start (run init_db()/migrations first) "
                f"rather than risk corrupting state with queries against columns that don't exist yet."
            )
        if db_version > migrations.CURRENT_SCHEMA_VERSION:
            logger.warning(
                "database schema_version=%s is newer than this worker's code (schema_version=%s) -- "
                "this worker was likely not yet upgraded in a rolling deployment; proceeding since all "
                "migrations are additive, but consider upgrading this worker soon.",
                db_version, migrations.CURRENT_SCHEMA_VERSION,
            )

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def run(self) -> None:
        init_db()
        self._check_schema_compatibility()
        workers_repo.upsert_worker(
            self.identity.worker_id, hostname=self.identity.hostname, pid=self.identity.pid,
            shard_index=self.shard_index, shard_count=self.shard_count, status=WorkerStatus.STARTING.value,
            worker_version=self.identity.worker_version, schema_version=self.identity.schema_version,
            capability_version=self.identity.capability_version, backend=self.identity.backend,
        )
        logger.info(
            "worker %s starting (shard %s/%s, backend=%s, schema_version=%s, worker_version=%s)",
            self.identity.worker_id, self.shard_index, self.shard_count,
            self.identity.backend, self.identity.schema_version, self.identity.worker_version,
        )
        try:
            while True:
                self._run_cycle()
                reaper.reap_orphans(stale_after_seconds=config.ORPHAN_WORKER_STALE_SECONDS)
                if self._stop.is_set() or self.single_cycle:
                    break
                self._heartbeat(WorkerStatus.IDLE)
                self._interruptible_sleep(self.idle_sleep_seconds)
                if self._stop.is_set():
                    break
        finally:
            self._heartbeat(WorkerStatus.STOPPED)
            logger.info(
                "worker %s stopped (portals_processed=%s jobs_processed=%s errors=%s)",
                self.identity.worker_id, self.portals_processed, self.jobs_processed, self.errors,
            )

    def _interruptible_sleep(self, seconds: float) -> None:
        self._stop.wait(timeout=seconds)

    def _heartbeat(self, status: WorkerStatus, *, portal_type: str = "", portal_id: Optional[int] = None) -> None:
        workers_repo.heartbeat_worker(
            self.identity.worker_id, status=status.value, current_portal_type=portal_type,
            current_portal_id=portal_id, portals_processed=self.portals_processed,
            jobs_processed=self.jobs_processed, errors=self.errors,
        )

    # --- one bounded cycle -----------------------------------------------

    def _run_cycle(self) -> dict:
        cycle_start = time.monotonic()
        budget = config.POLL_CYCLE_TIME_BUDGET_SECONDS
        max_portals = config.MAX_PORTALS_PER_WORKER_CYCLE
        processed_this_cycle = 0
        cycle_stats = dict(
            jobs_fetched=0, jobs_new=0, jobs_deduplicated=0, jobs_analyzed=0,
            confirmed_sponsors=0, likely_sponsors=0, hard_skips=0, packages_generated=0, errors=[],
        )
        providers_seen: set[str] = set()
        cycle_started_iso = datetime.now(timezone.utc).isoformat()
        db_cycle_id = start_discovery_cycle(cycle_started_iso, [])

        self._heartbeat(WorkerStatus.WORKING)
        last_heartbeat = time.monotonic()

        with ThreadPoolExecutor(max_workers=max(1, config.POLL_WORKER_CONCURRENCY)) as pool:
            while not self._stop.is_set():
                elapsed = time.monotonic() - cycle_start
                if elapsed >= budget or processed_this_cycle >= max_portals:
                    break
                remaining = max_portals - processed_this_cycle
                batch_limit = max(1, min(config.DUE_WORK_BATCH_SIZE, remaining))

                poll_items = self.poll_queue.claim_due_work(
                    worker_id=self.identity.worker_id, limit=batch_limit, lease_seconds=config.PORTAL_LEASE_SECONDS,
                    shard_count=self.shard_count, shard_index=self.shard_index,
                )
                verify_items = self.verify_queue.claim_due_work(
                    worker_id=self.identity.worker_id, limit=batch_limit, lease_seconds=config.PORTAL_LEASE_SECONDS,
                    shard_count=self.shard_count, shard_index=self.shard_index,
                )
                items: list[LeasedWorkItem] = poll_items + verify_items
                if not items:
                    break  # nothing due right now in this worker's shard

                futures = {
                    pool.submit(self._process_item, item, cycle_stats, db_cycle_id): item for item in items
                }
                for future in as_completed(futures):
                    item = futures[future]
                    try:
                        future.result()
                    except Exception:
                        logger.exception("unhandled error processing %s id=%s", item.portal_type, item.portal_id)
                        self.errors += 1
                    processed_this_cycle += 1
                    self.portals_processed += 1
                    providers_seen.add(item.provider)
                    if time.monotonic() - last_heartbeat >= config.WORKER_HEARTBEAT_SECONDS:
                        self._heartbeat(WorkerStatus.WORKING, portal_type=item.portal_type.value, portal_id=item.portal_id)
                        last_heartbeat = time.monotonic()
                    if processed_this_cycle >= max_portals:
                        break

        finished_iso = datetime.now(timezone.utc).isoformat()
        summary = {
            **cycle_stats,
            "started_at": cycle_started_iso, "finished_at": finished_iso,
            "duration_seconds": time.monotonic() - cycle_start, "providers": sorted(providers_seen),
        }
        finalize_discovery_cycle(db_cycle_id, summary)
        return summary

    # --- poll-queue item processing --------------------------------------

    def _process_item(self, item: LeasedWorkItem, cycle_stats: dict, db_cycle_id: int) -> None:
        if item.portal_type == PortalType.COMPANY_REGISTRY:
            self._process_poll_item(item, cycle_stats, db_cycle_id)
        else:
            self._process_verification_item(item)

    def _new_attempt(self, item: LeasedWorkItem, queue: str) -> AttemptRecord:
        return AttemptRecord(
            attempt_id=item.attempt_id, portal_type=item.portal_type.value, portal_id=item.portal_id,
            worker_id=self.identity.worker_id, provider=item.provider, queue=queue,
            started_at=utcnow(), status=AttemptStatus.RUNNING.value,
        )

    def _cooldown_skip(self, item: LeasedWorkItem) -> None:
        """Extends (rather than releases) the lease on an item that was
        cancelled without ever being attempted (circuit open / provider at
        its concurrency limit) -- see _SKIP_COOLDOWN_SECONDS docstring
        above. Falls back to a normal release if extension fails for some
        reason (e.g. the lease already expired), so the item is never lost."""
        from app.workers import leasing

        if item.portal_type == PortalType.COMPANY_REGISTRY:
            if not leasing.extend_poll_lease(item.portal_id, item.attempt_id, lease_seconds=_SKIP_COOLDOWN_SECONDS):
                self.poll_queue.retry(item)
        else:
            if not leasing.extend_verification_lease(item.portal_id, item.attempt_id, lease_seconds=_SKIP_COOLDOWN_SECONDS):
                self.verify_queue.retry(item)

    def _process_poll_item(self, item: LeasedWorkItem, cycle_stats: dict, db_cycle_id: int) -> None:
        attempt = self._new_attempt(item, "poll")

        if not circuit.may_attempt(item.provider):
            attempt.status = AttemptStatus.CANCELLED.value
            attempt.error_type = "circuit_open"
            attempt.detail = f"provider '{item.provider}' circuit breaker is OPEN -- skipped this cycle"
            attempt.finished_at = utcnow()
            workers_repo.record_attempt(attempt)
            self._cooldown_skip(item)
            return

        if not circuit.acquire_inflight_slot(item.provider):
            # may_attempt() above may have just claimed the single HALF_OPEN
            # probe slot for this provider -- free it here since this
            # attempt never actually runs, or a real probe would never get
            # another chance until the row-level lock frees itself.
            circuit.release_half_open_probe(item.provider)
            attempt.status = AttemptStatus.CANCELLED.value
            attempt.error_type = "provider_concurrency_limit"
            attempt.detail = f"provider '{item.provider}' is at its concurrency limit -- skipped this cycle"
            attempt.finished_at = utcnow()
            workers_repo.record_attempt(attempt)
            self._cooldown_skip(item)
            return

        t0 = time.monotonic()
        try:
            self._execute_poll(item, attempt, cycle_stats, db_cycle_id)
        except Exception as exc:  # noqa: BLE001 - final safety net: an attempt must ALWAYS
            # be recorded and the lease ALWAYS released, even for a failure
            # mode _execute_poll's own handling didn't anticipate (e.g. this
            # session's own live-testing caught a real one: a provider
            # connector's internal error isolation not covering every
            # exception type, letting ResponseTooLargeError escape
            # GreenhouseProvider.fetch_jobs() uncaught for an unusually
            # large real board -- without this net, that stranded the lease
            # with no attempt record at all until it expired).
            logger.exception("unexpected error executing poll for %s id=%s", item.portal_type, item.portal_id)
            self._handle_poll_probe_failure(item, attempt, exc, t0)
        finally:
            circuit.release_inflight_slot(item.provider)

    def _execute_poll(self, item: LeasedWorkItem, attempt: AttemptRecord, cycle_stats: dict, db_cycle_id: int) -> None:
        t0 = time.monotonic()
        client = None
        has_probe = probe_mod.has_probe(item.provider)
        try:
            if has_probe:
                client = httpx.Client(
                    timeout=httpx.Timeout(connect=config.PROVIDER_HTTP_TIMEOUT_SECONDS, read=config.PROVIDER_HTTP_TIMEOUT_SECONDS,
                                           write=config.PROVIDER_HTTP_TIMEOUT_SECONDS, pool=config.PROVIDER_HTTP_TIMEOUT_SECONDS),
                    headers={"User-Agent": config.PROVIDER_USER_AGENT},
                )
                response = probe_mod.probe(item.provider, item.tenant_identifier, client=client)
            else:
                # No raw structural probe for this provider (e.g. a provider
                # manually added to company_registry without going through
                # Phase 4 verification) -- fetch_jobs() below swallows its
                # own HTTP errors, so there is no real success/failure signal
                # to feed the circuit breaker here; skip it rather than
                # record a fabricated "success".
                response = None
        except Exception as exc:
            self._handle_poll_probe_failure(item, attempt, exc, t0)
            return
        finally:
            if client is not None:
                client.close()

        if has_probe:
            circuit.record_result(item.provider, success=True)
        else:
            # No real success/failure signal for this attempt (see above) --
            # still release any HALF_OPEN probe slot may_attempt() might
            # have claimed, so an unmeasurable provider can never wedge the
            # breaker open forever.
            circuit.release_half_open_probe(item.provider)
        registry_repo.update_entry(item.portal_id, consecutive_permanent_failures=0)

        if response is not None and schema_check.has_shape_check(item.provider):
            shape = schema_check.check_shape(item.provider, response)
            if not shape.ok:
                latency_ms = (time.monotonic() - t0) * 1000
                registry_repo.mark_poll_result(item.portal_id, success=True, jobs_new=0, latency_ms=latency_ms)
                schema_drift_repo.record_drift(
                    provider=item.provider, tenant_identifier=item.tenant_identifier, detail=shape.detail,
                )
                # CLAUDE.md Phase 6 section 17: drift affecting MANY tenants
                # of the same provider (not one oddball tenant) is fed into
                # the existing circuit breaker as a failure signal -- a
                # single tenant's drift never trips it on its own.
                distinct_tenants = schema_drift_repo.distinct_tenants_with_recent_drift(
                    item.provider, since_hours=config.SCHEMA_DRIFT_WINDOW_HOURS,
                )
                if distinct_tenants >= config.SCHEMA_DRIFT_CIRCUIT_TENANT_THRESHOLD:
                    circuit.record_result(item.provider, success=False)
                attempt.status = AttemptStatus.SUCCEEDED.value
                attempt.error_type = "schema_drift"
                attempt.detail = shape.detail
                attempt.latency_ms = latency_ms
                attempt.finished_at = utcnow()
                workers_repo.record_attempt(attempt)
                insert_discovery_log({
                    "cycle_id": db_cycle_id, "provider": item.provider, "company": item.company_name,
                    "tenant": item.tenant_identifier, "started_at": attempt.started_at, "finished_at": attempt.finished_at,
                    "latency_ms": latency_ms, "jobs_received": 0, "jobs_new": 0, "jobs_duplicate": 0,
                    "jobs_filtered": 0, "error_type": "SCHEMA_DRIFT",
                })
                self.poll_queue.ack(item)
                return

        provider_obj = build_provider_for_tenant(item.provider, item.tenant_identifier)
        if provider_obj is None:
            raw_jobs: list = []
        else:
            # CLAUDE.md Phase 6 sections 12-14: fetch_jobs_result() is the
            # structured counterpart to fetch_jobs() that actually
            # distinguishes "this tenant's fetch failed" from "this board is
            # genuinely empty" -- before this, a real fetch failure here was
            # invisible to the circuit breaker and attempt history (it just
            # looked like zero jobs). fetch_jobs() itself is untouched.
            fetch_result = provider_obj.fetch_jobs_result(config.MAX_JOBS_PER_PROVIDER, tenant=item.tenant_identifier)
            if not fetch_result.is_success:
                latency_ms = (time.monotonic() - t0) * 1000
                self._handle_poll_failure(
                    item, attempt, retryable=fetch_result.retryable, error_type=fetch_result.error_type or "unknown_failure",
                    detail=fetch_result.error_message_safe, latency_ms=latency_ms,
                )
                return
            raw_jobs = fetch_result.jobs
        jobs_received = len(raw_jobs)
        new_count = duplicate_count = filtered_count = 0

        for raw in raw_jobs:
            try:
                status = process_raw_job(
                    raw, cycle_stats, cycle_id=db_cycle_id, registry_id=item.portal_id,
                    correlation_id=item.attempt_id,
                )
            except Exception as exc:  # one bad job must never abort the whole attempt
                cycle_stats["errors"].append(f"{item.provider}/{raw.external_job_id}: {exc}")
                logger.exception("failed processing job %s/%s", item.provider, raw.external_job_id)
                continue
            cycle_stats["jobs_fetched"] += 1
            self.jobs_processed += 1
            if status == "new":
                new_count += 1
            elif status == "duplicate":
                duplicate_count += 1
            elif status == "filtered":
                filtered_count += 1

        latency_ms = (time.monotonic() - t0) * 1000
        registry_repo.mark_poll_result(item.portal_id, success=True, jobs_new=new_count, latency_ms=latency_ms)
        attempt.status = AttemptStatus.SUCCEEDED.value
        attempt.jobs_received = jobs_received
        attempt.jobs_new = new_count
        attempt.jobs_duplicate = duplicate_count
        attempt.jobs_filtered = filtered_count
        attempt.latency_ms = latency_ms
        attempt.finished_at = utcnow()
        workers_repo.record_attempt(attempt)
        insert_discovery_log({
            "cycle_id": db_cycle_id, "provider": item.provider, "company": item.company_name,
            "tenant": item.tenant_identifier, "started_at": attempt.started_at, "finished_at": attempt.finished_at,
            "latency_ms": latency_ms, "jobs_received": jobs_received, "jobs_new": new_count,
            "jobs_duplicate": duplicate_count, "jobs_filtered": filtered_count, "error_type": "",
        })
        logger.info(
            "poll attempt succeeded", extra={
                "worker_id": self.identity.worker_id, "attempt_id": attempt.attempt_id,
                "correlation_id": attempt.attempt_id, "portal_id": item.portal_id, "portal_type": item.portal_type.value,
                "provider": item.provider, "tenant": item.tenant_identifier, "duration_ms": round(latency_ms, 1),
                "event": "poll_succeeded",
            },
        )
        self.poll_queue.ack(item)

    def _handle_poll_probe_failure(self, item: LeasedWorkItem, attempt: AttemptRecord, exc: Exception, t0: float) -> None:
        latency_ms = (time.monotonic() - t0) * 1000
        retryable, error_type = retry.classify_exception(exc)
        self._handle_poll_failure(
            item, attempt, retryable=retryable, error_type=error_type, detail=str(exc), latency_ms=latency_ms,
        )

    def _handle_poll_failure(
        self, item: LeasedWorkItem, attempt: AttemptRecord, *,
        retryable: bool, error_type: str, detail: str, latency_ms: float,
    ) -> None:
        """Shared failure path for BOTH the structural-probe stage
        (exception-based, classified via app.workers.retry) and the
        fetch_jobs_result() stage (already classified into a
        ProviderFetchStatus) -- one place records the circuit-breaker
        result, backoff, dead-letter bookkeeping, and attempt history,
        regardless of which stage detected the failure."""
        circuit.record_result(item.provider, success=False)
        registry_repo.mark_poll_result(item.portal_id, success=False, jobs_new=0, latency_ms=latency_ms, error=detail)

        entry = registry_repo.get_entry(item.portal_id)
        consecutive_permanent = entry.consecutive_permanent_failures if entry else 0
        if not retryable:
            consecutive_permanent += 1
            registry_repo.update_entry(item.portal_id, consecutive_permanent_failures=consecutive_permanent)
            dead_letter.record_permanent_failure(
                portal_type="company_registry", portal_id=item.portal_id, provider=item.provider,
                consecutive_permanent_failures=consecutive_permanent, last_error=detail,
                last_attempt_id=item.attempt_id, threshold=config.DEAD_LETTER_MAX_ATTEMPTS,
            )

        # The actual next-attempt schedule is computed by mark_poll_result
        # above (app.registry.scheduling's deterministic backoff, already
        # applied to company_registry.next_poll_at) -- reflect that same
        # value here rather than recomputing it, so poll_attempts.next_retry_at
        # is never out of sync with what will really happen.
        refreshed_entry = registry_repo.get_entry(item.portal_id)

        attempt.status = AttemptStatus.RETRYABLE_FAILURE.value if retryable else AttemptStatus.PERMANENT_FAILURE.value
        attempt.error_type = error_type
        attempt.detail = detail[:500]
        attempt.retryable = retryable
        attempt.latency_ms = latency_ms
        attempt.finished_at = utcnow()
        attempt.next_retry_at = refreshed_entry.next_poll_at if refreshed_entry else None
        workers_repo.record_attempt(attempt)
        insert_discovery_log({
            "cycle_id": None, "provider": item.provider, "company": item.company_name,
            "tenant": item.tenant_identifier, "started_at": attempt.started_at, "finished_at": attempt.finished_at,
            "latency_ms": latency_ms, "jobs_received": 0, "jobs_new": 0, "jobs_duplicate": 0,
            "jobs_filtered": 0, "error_type": error_type,
        })
        logger.warning(
            "poll attempt failed", extra={
                "worker_id": self.identity.worker_id, "attempt_id": attempt.attempt_id,
                "correlation_id": attempt.attempt_id, "portal_id": item.portal_id, "portal_type": item.portal_type.value,
                "provider": item.provider, "tenant": item.tenant_identifier, "duration_ms": round(latency_ms, 1),
                "error_type": error_type, "event": "poll_failed",
            },
        )
        self.errors += 1
        if retryable:
            self.poll_queue.retry(item)
        else:
            self.poll_queue.fail(item)

    # --- verification-queue item processing -------------------------------

    def _process_verification_item(self, item: LeasedWorkItem) -> None:
        attempt = self._new_attempt(item, "verification")

        if not circuit.may_attempt(item.provider):
            attempt.status = AttemptStatus.CANCELLED.value
            attempt.error_type = "circuit_open"
            attempt.finished_at = utcnow()
            workers_repo.record_attempt(attempt)
            self._cooldown_skip(item)
            return

        if not circuit.acquire_inflight_slot(item.provider):
            circuit.release_half_open_probe(item.provider)
            attempt.status = AttemptStatus.CANCELLED.value
            attempt.error_type = "provider_concurrency_limit"
            attempt.finished_at = utcnow()
            workers_repo.record_attempt(attempt)
            self._cooldown_skip(item)
            return

        try:
            self._execute_verification(item, attempt)
        except Exception:  # noqa: BLE001 - safety net; verify_portal() is documented to never
            # raise, but a defensive net here still guarantees the lease is
            # never stranded and an attempt is always recorded, matching the
            # poll-queue side's equivalent protection above.
            logger.exception("unexpected error executing verification for portal id=%s", item.portal_id)
            attempt.status = AttemptStatus.RETRYABLE_FAILURE.value
            attempt.error_type = "unexpected_error"
            attempt.retryable = True
            attempt.finished_at = utcnow()
            workers_repo.record_attempt(attempt)
            self.verify_queue.retry(item)
        finally:
            circuit.release_inflight_slot(item.provider)

    def _execute_verification(self, item: LeasedWorkItem, attempt: AttemptRecord) -> None:
        t0 = time.monotonic()
        portal = registry_store.get_portal(item.portal_id)
        if portal is None:
            attempt.status = AttemptStatus.CANCELLED.value
            attempt.error_type = "portal_missing"
            attempt.finished_at = utcnow()
            workers_repo.record_attempt(attempt)
            self.verify_queue.ack(item)
            return

        company = registry_store.get_company(portal.company_id)
        outcome = verify_portal(portal, company_display_name=company.display_name if company else "")
        latency_ms = (time.monotonic() - t0) * 1000

        made_network_attempt = outcome.result != VerificationResult.UNSUPPORTED
        if made_network_attempt:
            circuit.record_result(item.provider, success=outcome.result != VerificationResult.TEMPORARY_FAILURE
                                   and outcome.result != VerificationResult.FAILED)
        else:
            # verify_portal() never made a request (UNSUPPORTED provider) --
            # release any HALF_OPEN probe slot may_attempt() claimed so this
            # can never wedge the breaker open.
            circuit.release_half_open_probe(item.provider)

        registry_lifecycle.apply_verification_outcome(item.portal_id, outcome)
        if outcome.result == VerificationResult.VERIFIED:
            registry_lifecycle.maybe_detect_migration(portal.company_id, registry_store.get_portal(item.portal_id))
        registry_sync.sync_portal_to_operational_registry(item.portal_id)

        updated_portal = registry_store.get_portal(item.portal_id)
        status_map = {
            VerificationResult.VERIFIED: AttemptStatus.SUCCEEDED,
            VerificationResult.AMBIGUOUS: AttemptStatus.SUCCEEDED,
            VerificationResult.UNSUPPORTED: AttemptStatus.PERMANENT_FAILURE,
            VerificationResult.FAILED: AttemptStatus.PERMANENT_FAILURE,
            VerificationResult.TEMPORARY_FAILURE: AttemptStatus.RETRYABLE_FAILURE,
        }
        attempt.status = status_map[outcome.result].value
        attempt.jobs_received = outcome.jobs_seen
        attempt.error_type = outcome.result.value
        attempt.detail = outcome.detail[:500]
        attempt.retryable = outcome.result == VerificationResult.TEMPORARY_FAILURE
        attempt.latency_ms = latency_ms
        attempt.finished_at = utcnow()
        workers_repo.record_attempt(attempt)

        if outcome.result == VerificationResult.FAILED and updated_portal is not None:
            dead_letter.record_permanent_failure(
                portal_type="registry_portal", portal_id=item.portal_id, provider=item.provider,
                consecutive_permanent_failures=updated_portal.consecutive_permanent_failures,
                last_error=outcome.detail, last_attempt_id=item.attempt_id,
                threshold=config.REGISTRY_STALE_AFTER_PERMANENT_FAILURES,
            )
            self.errors += 1

        terminal = updated_portal is not None and updated_portal.verification_status.value in _TERMINAL_VERIFICATION_STATES
        if terminal:
            self.verify_queue.ack(item)
        else:
            # Still pending (DISCOVERED/CANDIDATE) -- back off before this
            # portal becomes reclaimable again rather than hot-looping on a
            # transient failure or an unsupported provider that will never
            # change until the code does.
            attempt_number = max(1, (updated_portal.consecutive_permanent_failures if updated_portal else 0) + 1)
            backoff = retry.backoff_seconds(attempt_number, base_seconds=60.0, cap_seconds=21600.0)
            from app.workers import leasing
            leasing.extend_verification_lease(item.portal_id, item.attempt_id, lease_seconds=int(backoff))
