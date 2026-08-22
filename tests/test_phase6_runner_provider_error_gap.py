"""CLAUDE.md Phase 6 sections 12-15: the specific architectural gap Phase 5
left open -- a structural probe can succeed while the subsequent real
fetch_jobs() call fails (e.g. ResponseTooLargeError on an unusually large
real board), and before this fix that failure was invisible: fetch_jobs()
just returned [] and the attempt was recorded as a healthy SUCCEEDED empty
poll. This test proves the gap is now closed: the fetch-stage failure is
recorded as a real failure (circuit breaker fed, attempt marked
RETRYABLE_FAILURE, portal rescheduled with backoff), never as silent
success."""

import json

import httpx

from app.registry.models import CompanyRegistryEntry
from app.registry import repo as registry_repo
from app.workers import repo as workers_repo
from app.workers.runner import Worker


def _seed(provider="greenhouse", tenant="acme", name="Acme") -> int:
    return registry_repo.insert_entry(CompanyRegistryEntry(company_name=name, provider=provider, tenant_identifier=tenant))


def test_fetch_stage_failure_after_successful_probe_is_recorded_not_silenced(tmp_env, mock_httpx):
    call_count = {"n": 0}
    # A response big enough to trip PROVIDER_MAX_RESPONSE_BYTES on the
    # second (real fetch_jobs, content=true) call but not needed for the
    # tiny first (probe) call -- the probe's own response is small regardless.
    huge_jobs_payload = json.dumps({"jobs": [{"id": i, "title": "x" * 2000} for i in range(5000)]})

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Structural probe: small, healthy response.
            return httpx.Response(200, json={"jobs": [{"id": 1, "title": "Engineer"}]})
        # Real fetch_jobs() call: oversized, must trip ResponseTooLargeError.
        return httpx.Response(200, content=huge_jobs_payload)

    mock_httpx(handler)
    portal_id = _seed()

    w = Worker(single_cycle=True)
    summary = w._run_cycle()

    assert summary["jobs_new"] == 0
    attempts = workers_repo.list_recent_attempts(limit=10)
    assert len(attempts) == 1
    attempt = attempts[0]
    # Before this fix, this would have been SUCCEEDED with jobs_received=0 --
    # a silent false-healthy report of a real failure.
    assert attempt["status"] == "RETRYABLE_FAILURE"
    assert attempt["error_type"] == "RESPONSE_TOO_LARGE"

    entry = registry_repo.get_entry(portal_id)
    assert entry.consecutive_failures >= 1

    from app.workers import circuit

    status = circuit.get_status("greenhouse")
    assert status.consecutive_failures >= 1
