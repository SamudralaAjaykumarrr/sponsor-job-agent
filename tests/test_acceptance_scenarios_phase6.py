"""CLAUDE.md Phase 6 section 48: deterministic acceptance scenarios A-J.
Several are already covered by other Phase 6 test files -- this module
documents where each one lives and adds direct coverage for the two that
don't have a natural home elsewhere (C: provider-isolated rate limiting, G:
Postgres-unavailable readiness/claim-refusal).

  A. Two PostgreSQL workers claim 1,000 due portals, no double-claim
     -> tests/test_postgres_leasing.py::test_concurrent_workers_never_double_claim
     -> tests/test_multi_machine_simulation.py::test_multi_machine_simulation_on_postgres
  B. Worker dies mid-poll -> lease expires -> another worker completes, no dup job
     -> tests/test_postgres_leasing.py::test_crash_recovery_via_lease_expiry
     -> tests/test_workers_runner.py::test_worker_crash_recovery_lease_expires_and_is_reclaimed
  C. Provider 429 -> shared backoff, other providers continue -> THIS FILE
  D. Provider schema drift -> drift event, degraded health, empty jobs NOT success
     -> tests/test_phase6_schema_drift_persistence.py
     -> tests/test_schema_drift.py (Phase 5, unchanged)
  E. Valid zero-job board -> success, healthy, no drift error
     -> tests/test_workers_runner.py::test_empty_board_is_healthy_not_a_failure
  F. Duplicate queue delivery -> exactly one canonical job/application package
     -> tests/test_workers_runner.py::test_idempotent_retry_does_not_duplicate_jobs
     -> tests/test_phase6_distributed_acquisition.py::test_distributed_processing_creates_no_duplicate_companies
  G. PostgreSQL unavailable -> readiness false, no worker claims, clear failure -> THIS FILE
  H. SQLite local mode -> all current workflows still pass -> the entire
     default (non -m postgres) pytest run, 474 tests, is this proof.
  I. Large acquisition batch interrupted -> resumes from checkpoint, no dup companies
     -> tests/test_phase6_distributed_acquisition.py::test_reprocessing_a_claimed_but_crashed_row_after_lease_expiry_is_safe
     -> tests/test_registry_acquisition.py (Phase 5, unchanged single-process resume)
  J. Employer historical sponsorship evidence imported -> company signal only,
     current job stays UNKNOWN unless JD confirms -> THIS FILE
"""

import time
from datetime import datetime, timezone

import httpx
import pytest

from app.registry.models import CompanyRegistryEntry
from app.registry import repo as registry_repo
from app.workers import circuit
from app.workers import repo as workers_repo
from app.workers.runner import Worker

_NOW_ISO = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
_NOW_EPOCH_MS = int(time.time() * 1000)

GREENHOUSE_OK = {"jobs": [
    {"id": 1, "title": "Backend Engineer", "location": {"name": "Remote"}, "content": "desc",
     "absolute_url": "https://x/1", "updated_at": _NOW_ISO},
]}
LEVER_OK = [
    {"id": "l1", "text": "Platform Engineer", "categories": {"location": "Remote - US", "commitment": "Full-time"},
     "descriptionPlain": "Build backend platform services in Python.",
     "hostedUrl": "https://x/l1", "createdAt": _NOW_EPOCH_MS},
]


# --- Scenario C: provider 429 -> shared backoff, OTHER providers unaffected -

def test_scenario_c_provider_429_does_not_block_other_providers(tmp_env, mock_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        if "greenhouse" in str(request.url):
            return httpx.Response(429, headers={"Retry-After": "1"}, text="rate limited")
        return httpx.Response(200, json=LEVER_OK)

    mock_httpx(handler)
    registry_repo.insert_entry(CompanyRegistryEntry(company_name="GH Co", provider="greenhouse", tenant_identifier="gh1"))
    registry_repo.insert_entry(CompanyRegistryEntry(company_name="Lever Co", provider="lever", tenant_identifier="lv1"))

    w = Worker(single_cycle=True)
    w._run_cycle()

    gh_status = circuit.get_status("greenhouse")
    lever_status = circuit.get_status("lever")
    assert gh_status.consecutive_failures >= 1
    assert lever_status.state == "CLOSED"
    assert lever_status.consecutive_failures == 0

    attempts = workers_repo.list_recent_attempts(limit=10)
    gh_attempts = [a for a in attempts if a["provider"] == "greenhouse"]
    lever_attempts = [a for a in attempts if a["provider"] == "lever"]
    assert gh_attempts and gh_attempts[0]["status"] in ("RETRYABLE_FAILURE",)
    assert lever_attempts and lever_attempts[0]["status"] == "SUCCEEDED"

    from app.jobs_repo import list_jobs
    jobs = list_jobs({})
    assert any(j.provider == "lever" for j in jobs)
    assert not any(j.provider == "greenhouse" for j in jobs)


# --- Scenario G: PostgreSQL unavailable -> readiness false, claims refused -

@pytest.mark.postgres
def test_scenario_g_postgres_unavailable_readiness_is_false(monkeypatch):
    import app.db as db
    from app.health import check_readiness

    # A syntactically valid Postgres URL pointing at nothing listening --
    # connection must fail cleanly, not hang or crash the process.
    monkeypatch.setattr(db, "DATABASE_URL", "postgresql://user:pass@127.0.0.1:1/nonexistent_db")
    result = check_readiness()
    assert result.ready is False
    assert result.database_reachable is False
    assert "postgresql://" not in result.detail
    assert "pass" not in result.detail


@pytest.mark.postgres
def test_scenario_g_worker_refuses_to_claim_when_db_unreachable(monkeypatch):
    import app.db as db

    monkeypatch.setattr(db, "DATABASE_URL", "postgresql://user:pass@127.0.0.1:1/nonexistent_db")
    from app.workers.queue import SQLitePollQueue

    queue = SQLitePollQueue()
    with pytest.raises(Exception):
        queue.claim_due_work(worker_id="w1", limit=5, lease_seconds=60)


# --- Scenario J: historical sponsorship evidence never confirms a job -------

def test_scenario_j_sponsorship_evidence_does_not_promote_job_status(tmp_env):
    """Uses a company name NOT present in tmp_env's known_h1b_sponsors.json
    fixture list ("Acme Corp"/"Globex"/"Initech"), so this test isolates the
    NEW employer_sponsorship_evidence table's effect (must be none on
    classification) from the pre-existing known-sponsors-file mechanism
    (which legitimately DOES yield LIKELY_SPONSOR -- that's Phase 2
    behavior, unchanged, and not what this test is about)."""
    from app.sponsorship.classifier import classify_sponsorship
    from app.sponsorship.evidence import SponsorshipEvidence, record_evidence

    record_evidence(SponsorshipEvidence(
        company_name_raw="Unrelated Evidence Co", source="USER_SUPPLIED", fiscal_year=2025,
        petition_type="H-1B", confidence=90, source_quality="OFFICIAL_GOV_DATA",
    ))

    # JD text says nothing explicit about sponsorship -- must stay UNKNOWN
    # even though the SAME company has strong historical evidence on file
    # in the NEW Phase 6 evidence table (which classify_sponsorship never
    # even imports/reads).
    status, evidence_text = classify_sponsorship(
        "Build backend services in Python. 3+ years experience required.", "Unrelated Evidence Co",
    )
    from app.models import SponsorshipStatus

    assert status == SponsorshipStatus.UNKNOWN
    assert "H-1B" not in evidence_text
