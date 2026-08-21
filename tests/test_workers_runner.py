import httpx

from app import config
from app.registry.models import CareerPortal, Company, CompanyRegistryEntry, PortalStatus
from app.registry import repo as registry_repo
from app.registry import store as registry_store
from app.workers import circuit, repo as workers_repo
from app.workers.leasing import claim_poll_batch
from app.workers.runner import Worker

GREENHOUSE_OK = {"jobs": [
    {"id": 111, "title": "Backend Software Engineer", "location": {"name": "Remote - US"},
     "content": "We sponsor H-1B. Python FastAPI backend. Full-time.",
     "absolute_url": "https://boards.greenhouse.io/acme/jobs/111", "updated_at": "2026-08-21T10:00:00Z",
     "departments": [{"name": "Engineering"}]},
]}


def _seed(provider="greenhouse", tenant="acme", name="Acme") -> int:
    return registry_repo.insert_entry(CompanyRegistryEntry(company_name=name, provider=provider, tenant_identifier=tenant))


def test_successful_poll_stores_job_and_records_attempt(tmp_env, mock_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=GREENHOUSE_OK)

    mock_httpx(handler)
    _seed()
    w = Worker(single_cycle=True)
    summary = w._run_cycle()

    assert summary["jobs_new"] == 1
    attempts = workers_repo.list_recent_attempts(limit=10)
    assert len(attempts) == 1
    assert attempts[0]["status"] == "SUCCEEDED"
    assert attempts[0]["jobs_new"] == 1


def test_empty_board_is_healthy_not_a_failure(tmp_env, mock_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jobs": []})

    mock_httpx(handler)
    entry_id = _seed()
    w = Worker(single_cycle=True)
    w._run_cycle()

    attempts = workers_repo.list_recent_attempts(limit=10)
    assert attempts[0]["status"] == "SUCCEEDED"
    assert attempts[0]["error_type"] == ""
    entry = registry_repo.get_entry(entry_id)
    assert entry.enabled is True
    assert entry.consecutive_failures == 0


def test_schema_drift_is_recorded_distinctly_from_empty_board(tmp_env, mock_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"totally": "wrong shape"})

    mock_httpx(handler)
    entry_id = _seed()
    w = Worker(single_cycle=True)
    w._run_cycle()

    attempts = workers_repo.list_recent_attempts(limit=10)
    assert attempts[0]["status"] == "SUCCEEDED"
    assert attempts[0]["error_type"] == "schema_drift"
    assert attempts[0]["jobs_received"] == 0
    entry = registry_repo.get_entry(entry_id)
    assert entry.enabled is True  # schema drift never quarantines/disables


def test_permanent_failure_recorded_and_counted(tmp_env, mock_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    mock_httpx(handler)
    entry_id = _seed()
    w = Worker(single_cycle=True)
    w._run_cycle()

    attempts = workers_repo.list_recent_attempts(limit=10)
    assert attempts[0]["status"] == "PERMANENT_FAILURE"
    assert attempts[0]["retryable"] == 0
    entry = registry_repo.get_entry(entry_id)
    assert entry.consecutive_permanent_failures == 1
    assert entry.enabled is True  # below dead-letter threshold


def test_permanent_failures_dead_letter_after_threshold(tmp_env, mock_httpx, monkeypatch):
    monkeypatch.setattr(config, "DEAD_LETTER_MAX_ATTEMPTS", 2)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    mock_httpx(handler)
    entry_id = _seed()
    w = Worker(single_cycle=True)
    w._run_cycle()
    # Force it due again immediately -- otherwise the real exponential
    # backoff from the first failure correctly pushes next_poll_at into the
    # future, and a second cycle run back-to-back would see nothing due yet.
    registry_repo.update_entry(entry_id, next_poll_at=None)
    w._run_cycle()

    entry = registry_repo.get_entry(entry_id)
    assert entry.enabled is False
    from app.workers import repo as wrepo
    dl = wrepo.get_open_dead_letter("company_registry", entry_id)
    assert dl is not None
    assert dl["attempt_count"] == 2

    # A third cycle must not even attempt the now-disabled, dead-lettered entry.
    w._run_cycle()
    attempts = workers_repo.list_attempts_for_portal("company_registry", entry_id, limit=10)
    assert len(attempts) == 2


def test_retryable_failure_backs_off_via_existing_scheduling(tmp_env, mock_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    mock_httpx(handler)
    entry_id = _seed()
    before = registry_repo.get_entry(entry_id)
    w = Worker(single_cycle=True)
    w._run_cycle()

    attempts = workers_repo.list_recent_attempts(limit=10)
    assert attempts[0]["status"] == "RETRYABLE_FAILURE"
    after = registry_repo.get_entry(entry_id)
    assert after.consecutive_failures == 1
    assert after.next_poll_at > before.next_poll_at if before.next_poll_at else True
    assert after.consecutive_permanent_failures == 0  # never counted as permanent


def test_unexpected_fetch_jobs_exception_still_records_attempt_and_releases_lease(tmp_env, mock_httpx, monkeypatch):
    """Regression test for a real failure mode caught during this phase's own
    live validation: a provider connector's internal error isolation didn't
    cover every exception type (ResponseTooLargeError escaped
    GreenhouseProvider.fetch_jobs() uncaught for an unusually large real
    board), which stranded the lease with zero attempt record until it
    expired. The worker's outer safety net must catch ANY exception from
    fetch_jobs(), not just the ones the probe layer already anticipates."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jobs": []})  # probe succeeds

    mock_httpx(handler)
    entry_id = _seed()

    class ExplodingProvider:
        def fetch_jobs(self, max_jobs):
            raise RuntimeError("simulated unexpected connector failure")

    # monkeypatch (not a manual assign-and-restore) guarantees this is
    # reverted after the test regardless of outcome -- a manual restore here
    # once left runner.py's own imported reference permanently overwritten
    # for the rest of the pytest session, silently breaking every later test
    # that executes a poll.
    import app.workers.runner as runner_mod

    monkeypatch.setattr(runner_mod, "build_provider_for_tenant", lambda provider, tenant: ExplodingProvider())

    w = Worker(single_cycle=True)
    w._run_cycle()

    attempts = workers_repo.list_attempts_for_portal("company_registry", entry_id, limit=10)
    assert len(attempts) == 1, "an attempt must always be recorded, even for a wholly unanticipated exception"
    assert attempts[0]["status"] in ("RETRYABLE_FAILURE", "PERMANENT_FAILURE")
    assert attempts[0]["error_type"] == "unexpected_error"

    entry = registry_repo.get_entry(entry_id)
    assert entry.lease_owner is None, "lease must never be stranded, regardless of failure cause"


def test_idempotent_retry_does_not_duplicate_jobs(tmp_env, mock_httpx):
    """Same portal polled twice in a row (e.g. a retried cycle after the
    first succeeded) must not create a duplicate canonical job."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=GREENHOUSE_OK)

    mock_httpx(handler)
    _seed()
    w = Worker(single_cycle=True)
    w._run_cycle()

    # Force it due again immediately for a second cycle.
    registry_repo.update_entry(1, next_poll_at=None)
    summary2 = w._run_cycle()
    assert summary2["jobs_new"] == 0
    assert summary2["jobs_deduplicated"] == 1

    from app.jobs_repo import list_jobs
    assert len(list_jobs()) == 1


def test_worker_crash_recovery_lease_expires_and_is_reclaimed(tmp_env, mock_httpx):
    """Simulates: worker A leases a portal and crashes before completing (its
    lease is never released). Once the lease TTL passes, worker B can claim
    and complete the SAME portal successfully -- no work is permanently lost,
    and no duplicate canonical job is created."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=GREENHOUSE_OK)

    mock_httpx(handler)
    entry_id = _seed()

    # Worker A "crashes": claims the lease directly (bypassing normal
    # execution) and never does anything else with it.
    crashed = claim_poll_batch(worker_id="worker-A-crashed", limit=1, lease_seconds=-1)  # already expired
    assert len(crashed) == 1

    # Worker B runs a normal cycle -- the expired lease must be reclaimable.
    w = Worker(single_cycle=True)
    summary = w._run_cycle()
    assert summary["jobs_new"] == 1

    attempts = workers_repo.list_attempts_for_portal("company_registry", entry_id, limit=10)
    assert len(attempts) == 1  # worker A never recorded an attempt (it "crashed" before doing so)
    assert attempts[0]["worker_id"] == w.identity.worker_id
    assert attempts[0]["status"] == "SUCCEEDED"

    from app.jobs_repo import list_jobs
    assert len(list_jobs()) == 1  # no duplicate canonical job despite the crash+reclaim

    entry = registry_repo.get_entry(entry_id)
    assert entry.lease_owner is None  # cleanly released after worker B's success


def test_circuit_open_skips_portal_without_attempt(tmp_env, mock_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=GREENHOUSE_OK)

    mock_httpx(handler)
    for _ in range(6):
        circuit.record_result("greenhouse", success=False)
    assert circuit.get_status("greenhouse").state == "OPEN"

    entry_id = _seed()
    w = Worker(single_cycle=True)
    w._run_cycle()

    attempts = workers_repo.list_attempts_for_portal("company_registry", entry_id, limit=10)
    assert attempts[0]["status"] == "CANCELLED"
    assert attempts[0]["error_type"] == "circuit_open"
    entry = registry_repo.get_entry(entry_id)
    # Given a short cooldown rather than released outright -- prevents a
    # busy-spin of claim/cancel/reclaim while the circuit stays open, but
    # the row is still owned by this worker and not permanently stuck (its
    # lease has a bounded expiry, unlike a portal a crashed worker forgot).
    assert entry.lease_owner == w.identity.worker_id
    assert entry.lease_expires_at is not None


def test_provider_isolation_one_failing_provider_does_not_block_another(tmp_env, mock_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        if "boards-api.greenhouse.io" in str(request.url):
            return httpx.Response(500, text="down")
        return httpx.Response(200, json=[])  # lever: healthy, empty board

    mock_httpx(handler)
    _seed(provider="greenhouse", tenant="broken")
    _seed(provider="lever", tenant="healthy", name="Healthy Co")

    w = Worker(single_cycle=True)
    w._run_cycle()

    attempts = {a["provider"]: a for a in workers_repo.list_recent_attempts(limit=10)}
    assert attempts["greenhouse"]["status"] == "RETRYABLE_FAILURE"
    assert attempts["lever"]["status"] == "SUCCEEDED"


def test_graceful_stop_prevents_claiming_new_work(tmp_env, mock_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=GREENHOUSE_OK)

    mock_httpx(handler)
    _seed()
    w = Worker(single_cycle=True)
    w.request_stop()
    summary = w._run_cycle()
    assert summary["jobs_new"] == 0
    assert workers_repo.list_recent_attempts(limit=10) == []


def test_heartbeat_and_final_status_recorded(tmp_env, mock_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=GREENHOUSE_OK)

    mock_httpx(handler)
    _seed()
    w = Worker(single_cycle=True, idle_sleep_seconds=0)
    w.run()

    worker_row = workers_repo.get_worker(w.identity.worker_id)
    assert worker_row is not None
    assert worker_row["status"] == "STOPPED"
    assert worker_row["portals_processed"] == 1
    assert worker_row["jobs_processed"] == 1


# --- verification queue ------------------------------------------------

def test_verification_queue_promotes_verified_portal_and_syncs(tmp_env, mock_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=GREENHOUSE_OK)

    mock_httpx(handler)
    company_id = registry_store.insert_company(Company(normalized_name="acme", display_name="Acme Widgets", primary_domain="acme.com"))
    portal_id = registry_store.insert_portal(CareerPortal(
        company_id=company_id, provider="greenhouse", tenant_identifier="acme",
        verification_status=PortalStatus.CANDIDATE,
    ))

    w = Worker(single_cycle=True)
    w._run_cycle()

    portal = registry_store.get_portal(portal_id)
    assert portal.verification_status == PortalStatus.ACTIVE
    assert portal.registry_entry_id is not None

    attempts = workers_repo.list_attempts_for_portal("registry_portal", portal_id, limit=10)
    assert attempts[0]["status"] == "SUCCEEDED"
    assert attempts[0]["queue"] == "verification"


def test_verification_queue_failed_portal_backs_off_not_hot_loop(tmp_env, mock_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    mock_httpx(handler)
    company_id = registry_store.insert_company(Company(normalized_name="bad", display_name="Bad Co", primary_domain="bad.com"))
    portal_id = registry_store.insert_portal(CareerPortal(
        company_id=company_id, provider="greenhouse", tenant_identifier="doesnotexist",
        verification_status=PortalStatus.CANDIDATE,
    ))

    w = Worker(single_cycle=True)
    w._run_cycle()

    portal = registry_store.get_portal(portal_id)
    assert portal.verification_status == PortalStatus.CANDIDATE  # not yet at demotion threshold
    assert portal.verify_lease_expires_at is not None  # backed off, not immediately reclaimable

    # A second cycle right away must NOT re-attempt it (still cooling down).
    w2 = Worker(single_cycle=True)
    w2._run_cycle()
    attempts = workers_repo.list_attempts_for_portal("registry_portal", portal_id, limit=10)
    assert len(attempts) == 1
