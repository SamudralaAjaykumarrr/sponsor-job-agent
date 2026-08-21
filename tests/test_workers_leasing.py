import threading
import time

from app.registry.models import CareerPortal, Company, CompanyRegistryEntry, PortalStatus
from app.registry import repo as registry_repo
from app.registry import store as registry_store
from app.workers import leasing


def _seed_entries(n: int) -> list[int]:
    return [
        registry_repo.insert_entry(CompanyRegistryEntry(company_name=f"C{i}", provider="greenhouse", tenant_identifier=f"t{i}"))
        for i in range(n)
    ]


def test_claim_poll_batch_is_atomic_and_exclusive(tmp_env):
    ids = _seed_entries(10)
    first = leasing.claim_poll_batch(worker_id="w1", limit=10, lease_seconds=60)
    assert {r["id"] for r in first} == set(ids)
    # A second worker claiming immediately after gets nothing -- everything
    # is already leased and not yet expired.
    second = leasing.claim_poll_batch(worker_id="w2", limit=10, lease_seconds=60)
    assert second == []


def test_claim_poll_batch_no_duplicates_across_concurrent_threads(tmp_env):
    ids = _seed_entries(30)
    results: dict[str, list[int]] = {}
    lock = threading.Lock()

    def worker(wid: str) -> None:
        claimed = leasing.claim_poll_batch(worker_id=wid, limit=30, lease_seconds=60)
        with lock:
            results[wid] = [c["id"] for c in claimed]

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    all_claimed = [i for ids_ in results.values() for i in ids_]
    assert len(all_claimed) == len(set(all_claimed)) == len(ids)


def test_expired_lease_is_reclaimable(tmp_env):
    _seed_entries(1)
    first = leasing.claim_poll_batch(worker_id="w1", limit=1, lease_seconds=-1)  # already expired
    assert len(first) == 1
    second = leasing.claim_poll_batch(worker_id="w2", limit=1, lease_seconds=60)
    assert len(second) == 1
    assert second[0]["lease_owner"] == "w2"


def test_release_poll_lease_makes_row_immediately_reclaimable(tmp_env):
    ids = _seed_entries(1)
    claimed = leasing.claim_poll_batch(worker_id="w1", limit=1, lease_seconds=300)
    leasing.release_poll_lease(ids[0], expected_attempt_id=claimed[0]["lease_attempt_id"])
    second = leasing.claim_poll_batch(worker_id="w2", limit=1, lease_seconds=60)
    assert len(second) == 1


def test_release_guarded_by_attempt_id_cannot_release_someone_elses_lease(tmp_env):
    """Simulates a worker whose lease already expired and was reclaimed by
    another worker -- its stale release call must be a no-op."""
    ids = _seed_entries(1)
    leasing.claim_poll_batch(worker_id="w1", limit=1, lease_seconds=-1)  # w1's lease already expired
    reclaimed = leasing.claim_poll_batch(worker_id="w2", limit=1, lease_seconds=300)
    assert reclaimed[0]["lease_owner"] == "w2"

    # w1 (unaware it lost the lease) tries to release using its OLD attempt_id.
    leasing.release_poll_lease(ids[0], expected_attempt_id="stale-attempt-id-from-w1")

    entry = registry_repo.get_entry(ids[0])
    assert entry.lease_owner == "w2"  # untouched -- w1's stale release had no effect


def test_extend_poll_lease_renews_without_losing_ownership(tmp_env):
    ids = _seed_entries(1)
    claimed = leasing.claim_poll_batch(worker_id="w1", limit=1, lease_seconds=5)
    attempt_id = claimed[0]["lease_attempt_id"]
    ok = leasing.extend_poll_lease(ids[0], attempt_id, lease_seconds=600)
    assert ok is True
    entry = registry_repo.get_entry(ids[0])
    assert entry.lease_expires_at > claimed[0]["lease_expires_at"]


def test_extend_lease_fails_once_lease_is_lost(tmp_env):
    ids = _seed_entries(1)
    claimed = leasing.claim_poll_batch(worker_id="w1", limit=1, lease_seconds=-1)
    leasing.claim_poll_batch(worker_id="w2", limit=1, lease_seconds=300)  # w2 reclaims
    ok = leasing.extend_poll_lease(ids[0], claimed[0]["lease_attempt_id"], lease_seconds=600)
    assert ok is False


def test_claim_respects_shard_assignment(tmp_env):
    ids = _seed_entries(50)
    claimed_shard0 = leasing.claim_poll_batch(worker_id="w0", limit=50, lease_seconds=60, shard_count=4, shard_index=0)
    # Release so other shards can see the same rows (a real deployment would
    # have distinct never-overlapping rows per worker, but for the isolation
    # property we just need to confirm every claim from shard 0 truly maps
    # to shard 0, and shards partition without overlap).
    from app.registry.sharding import shard_for_portal

    for row in claimed_shard0:
        assert shard_for_portal(row["id"], 4) == 0


def test_shard_partition_covers_every_portal_exactly_once(tmp_env):
    from app.registry.sharding import shard_for_portal

    ids = _seed_entries(97)  # deliberately not evenly divisible by 4
    shard_count = 4
    assignment = {i: shard_for_portal(i, shard_count) for i in ids}
    for shard_index in range(shard_count):
        members = {i for i, s in assignment.items() if s == shard_index}
        # deterministic + stable across repeated calls
        for i in members:
            assert shard_for_portal(i, shard_count) == shard_index
    covered = set()
    for shard_index in range(shard_count):
        covered |= {i for i, s in assignment.items() if s == shard_index}
    assert covered == set(ids)  # every portal maps to exactly one shard


def test_claim_verification_batch_atomic_and_exclusive(tmp_env):
    company_id = registry_store.insert_company(Company(normalized_name="acme", display_name="Acme", primary_domain="acme.com"))
    portal_ids = [
        registry_store.insert_portal(CareerPortal(company_id=company_id, provider="greenhouse", tenant_identifier=f"t{i}",
                                                    verification_status=PortalStatus.CANDIDATE))
        for i in range(5)
    ]
    first = leasing.claim_verification_batch(worker_id="v1", limit=5, lease_seconds=60)
    assert {r["id"] for r in first} == set(portal_ids)
    second = leasing.claim_verification_batch(worker_id="v2", limit=5, lease_seconds=60)
    assert second == []
