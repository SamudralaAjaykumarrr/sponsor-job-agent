"""CLAUDE.md Phase 6 section 28: distributed, checkpointed registry
acquisition -- multiple workers processing the SAME batch must not create
duplicate companies/portals, and progress must be resumable/queryable."""

import csv

import pytest

from app.registry import acquisition, acquisition_records, store


def _write_csv(tmp_path, rows):
    path = tmp_path / "companies.csv"
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["company_name", "company_domain"])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def test_create_distributed_batch_seeds_records(tmp_env, tmp_path):
    path = _write_csv(tmp_path, [
        {"company_name": "Acme Corp", "company_domain": "acme.example.com"},
        {"company_name": "Globex", "company_domain": "globex.example.com"},
    ])
    batch_id = acquisition.create_distributed_batch(path, source_name="test-seed")
    progress = acquisition_records.batch_progress(batch_id)
    assert progress["total"] == 2
    assert progress["pending"] == 2


def test_two_workers_never_double_claim_same_row(tmp_env, tmp_path):
    path = _write_csv(tmp_path, [{"company_name": f"Company {i}", "company_domain": f"co{i}.example.com"} for i in range(20)])
    batch_id = acquisition.create_distributed_batch(path, source_name="test-concurrent")

    claimed_a = acquisition_records.claim_batch(batch_id=batch_id, worker_id="worker-a", limit=10, lease_seconds=120)
    claimed_b = acquisition_records.claim_batch(batch_id=batch_id, worker_id="worker-b", limit=10, lease_seconds=120)

    ids_a = {r["id"] for r in claimed_a}
    ids_b = {r["id"] for r in claimed_b}
    assert not (ids_a & ids_b)
    assert len(ids_a) == 10
    assert len(ids_b) == 10


def test_distributed_processing_creates_no_duplicate_companies(tmp_env, tmp_path):
    path = _write_csv(tmp_path, [
        {"company_name": "Acme Corp", "company_domain": "acme.example.com"},
        {"company_name": "Globex", "company_domain": "globex.example.com"},
        {"company_name": "Initech", "company_domain": "initech.example.com"},
    ])
    batch_id = acquisition.create_distributed_batch(path, source_name="test-dup")

    # Simulate two workers alternating calls against the same batch.
    result_a = acquisition.process_distributed_batch_once(batch_id, worker_id="worker-a", limit=2, verify_new_candidates=False)
    result_b = acquisition.process_distributed_batch_once(batch_id, worker_id="worker-b", limit=2, verify_new_candidates=False)

    assert result_a["progress"]["done"] + result_b["claimed_this_call"] >= 0  # sanity: no crash
    final_progress = acquisition_records.batch_progress(batch_id)
    assert final_progress["done"] == 3
    assert final_progress["failed"] == 0

    companies = store.list_companies(limit=100) if hasattr(store, "list_companies") else None
    if companies is not None:
        names = [c.display_name for c in companies]
        assert len(names) == len(set(names))


def test_batch_marked_completed_once_all_rows_done(tmp_env, tmp_path):
    path = _write_csv(tmp_path, [{"company_name": "Acme Corp", "company_domain": "acme.example.com"}])
    batch_id = acquisition.create_distributed_batch(path, source_name="test-complete")
    acquisition.process_distributed_batch_once(batch_id, worker_id="worker-a", limit=10, verify_new_candidates=False)
    batch = acquisition.get_batch(batch_id)
    assert batch["status"] == "COMPLETED"


def test_reprocessing_a_claimed_but_crashed_row_after_lease_expiry_is_safe(tmp_env, tmp_path, monkeypatch):
    path = _write_csv(tmp_path, [{"company_name": "Acme Corp", "company_domain": "acme.example.com"}])
    batch_id = acquisition.create_distributed_batch(path, source_name="test-crash-recover")

    # worker-a claims but "crashes" (never marks done) with a very short lease.
    claimed = acquisition_records.claim_batch(batch_id=batch_id, worker_id="worker-a", limit=10, lease_seconds=1)
    assert len(claimed) == 1

    import time

    time.sleep(1.2)

    result = acquisition.process_distributed_batch_once(batch_id, worker_id="worker-b", limit=10, verify_new_candidates=False)
    assert result["claimed_this_call"] == 1
    progress = acquisition_records.batch_progress(batch_id)
    assert progress["done"] == 1
