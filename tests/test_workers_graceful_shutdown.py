import threading
import time

import httpx

from app.registry.models import CompanyRegistryEntry
from app.registry import repo as registry_repo
from app.workers import repo as workers_repo
from app.workers.leasing import claim_poll_batch
from app.workers.models import WorkerStatus
from app.workers.runner import Worker


def _seed(n: int) -> list[int]:
    return [
        registry_repo.insert_entry(CompanyRegistryEntry(company_name=f"C{i}", provider="greenhouse", tenant_identifier=f"t{i}"))
        for i in range(n)
    ]


def test_request_stop_before_run_prevents_any_cycle(tmp_env, mock_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jobs": []})

    mock_httpx(handler)
    _seed(5)
    w = Worker(idle_sleep_seconds=0)
    w.request_stop()
    w.run()  # not single_cycle -- but stop is already set, so the loop must exit after one bounded cycle

    worker_row = workers_repo.get_worker(w.identity.worker_id)
    assert worker_row["status"] == "STOPPED"


def test_stop_during_idle_wait_exits_promptly(tmp_env, mock_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jobs": []})

    mock_httpx(handler)
    _seed(1)
    w = Worker(idle_sleep_seconds=30)  # would otherwise sleep 30s between cycles

    run_thread = threading.Thread(target=w.run)
    run_thread.start()
    time.sleep(0.3)  # let it finish its first cycle and enter the idle wait
    w.request_stop()
    run_thread.join(timeout=5)

    assert not run_thread.is_alive(), "worker did not stop promptly once idle-waiting"
    worker_row = workers_repo.get_worker(w.identity.worker_id)
    assert worker_row["status"] == "STOPPED"


def test_signal_handler_triggers_stop(tmp_env):
    import signal

    w = Worker(single_cycle=True)
    w._handle_signal(signal.SIGTERM, None)
    assert w.stopping is True


def test_stopped_worker_never_claims_new_work_leaving_leases_recoverable(tmp_env, mock_httpx):
    """A lease held when a worker is asked to stop mid-attempt is left for
    another worker to reclaim once it expires -- graceful shutdown does not
    corrupt or strand it."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jobs": []})

    mock_httpx(handler)
    entry_id = _seed(1)[0]

    claim_poll_batch(worker_id="worker-stopping", limit=1, lease_seconds=-1)  # expired immediately, like a crash/stop

    w = Worker(single_cycle=True)
    w.request_stop()
    w._run_cycle()  # claims no NEW work since stop is already set

    entry = registry_repo.get_entry(entry_id)
    assert entry.lease_owner == "worker-stopping"  # left exactly as-is, recoverable by expiry


def test_full_shutdown_lifecycle_status_sequence(tmp_env, mock_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jobs": []})

    mock_httpx(handler)
    _seed(1)
    w = Worker(single_cycle=True, idle_sleep_seconds=0)
    w.run()
    worker_row = workers_repo.get_worker(w.identity.worker_id)
    assert worker_row["status"] == WorkerStatus.STOPPED.value
