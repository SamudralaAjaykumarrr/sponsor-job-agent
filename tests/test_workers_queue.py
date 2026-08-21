from app.registry.models import CareerPortal, Company, CompanyRegistryEntry, PortalStatus
from app.registry import repo as registry_repo
from app.registry import store as registry_store
from app.workers.queue import SQLitePollQueue, SQLiteVerificationQueue


def test_poll_queue_claim_ack_cycle(tmp_env):
    registry_repo.insert_entry(CompanyRegistryEntry(company_name="Acme", provider="greenhouse", tenant_identifier="acme"))
    q = SQLitePollQueue()
    items = q.claim_due_work(worker_id="w1", limit=10, lease_seconds=60)
    assert len(items) == 1
    q.ack(items[0])
    # Released -- immediately reclaimable.
    again = q.claim_due_work(worker_id="w2", limit=10, lease_seconds=60)
    assert len(again) == 1


def test_poll_queue_retry_releases_lease(tmp_env):
    registry_repo.insert_entry(CompanyRegistryEntry(company_name="Acme", provider="greenhouse", tenant_identifier="acme"))
    q = SQLitePollQueue()
    items = q.claim_due_work(worker_id="w1", limit=10, lease_seconds=300)
    q.retry(items[0])
    again = q.claim_due_work(worker_id="w2", limit=10, lease_seconds=60)
    assert len(again) == 1


def test_poll_queue_extend_lease(tmp_env):
    registry_repo.insert_entry(CompanyRegistryEntry(company_name="Acme", provider="greenhouse", tenant_identifier="acme"))
    q = SQLitePollQueue()
    items = q.claim_due_work(worker_id="w1", limit=10, lease_seconds=5)
    assert q.extend_lease(items[0], lease_seconds=600) is True


def test_verification_queue_claim_ack_cycle(tmp_env):
    company_id = registry_store.insert_company(Company(normalized_name="acme", display_name="Acme", primary_domain="acme.com"))
    registry_store.insert_portal(CareerPortal(company_id=company_id, provider="greenhouse", tenant_identifier="acme",
                                               verification_status=PortalStatus.CANDIDATE))
    q = SQLiteVerificationQueue()
    items = q.claim_due_work(worker_id="v1", limit=10, lease_seconds=60)
    assert len(items) == 1
    q.ack(items[0])
    again = q.claim_due_work(worker_id="v2", limit=10, lease_seconds=60)
    assert len(again) == 1


def test_verification_queue_does_not_claim_verified_portals(tmp_env):
    company_id = registry_store.insert_company(Company(normalized_name="acme", display_name="Acme", primary_domain="acme.com"))
    registry_store.insert_portal(CareerPortal(company_id=company_id, provider="greenhouse", tenant_identifier="acme",
                                               verification_status=PortalStatus.ACTIVE))
    q = SQLiteVerificationQueue()
    assert q.claim_due_work(worker_id="v1", limit=10, lease_seconds=60) == []
