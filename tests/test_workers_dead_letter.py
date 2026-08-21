from app import config
from app.registry.models import CompanyRegistryEntry
from app.registry import repo as registry_repo
from app.workers import dead_letter, repo as workers_repo


def test_record_permanent_failure_below_threshold_does_nothing(tmp_env):
    entry_id = registry_repo.insert_entry(CompanyRegistryEntry(company_name="Acme", provider="greenhouse", tenant_identifier="acme"))
    dead_lettered = dead_letter.record_permanent_failure(
        portal_type="company_registry", portal_id=entry_id, provider="greenhouse",
        consecutive_permanent_failures=2, last_error="404", last_attempt_id="a1", threshold=8,
    )
    assert dead_lettered is False
    assert workers_repo.get_open_dead_letter("company_registry", entry_id) is None
    entry = registry_repo.get_entry(entry_id)
    assert entry.enabled is True


def test_record_permanent_failure_at_threshold_dead_letters_and_disables(tmp_env):
    entry_id = registry_repo.insert_entry(CompanyRegistryEntry(company_name="Acme", provider="greenhouse", tenant_identifier="acme"))
    dead_lettered = dead_letter.record_permanent_failure(
        portal_type="company_registry", portal_id=entry_id, provider="greenhouse",
        consecutive_permanent_failures=8, last_error="404 not found", last_attempt_id="a1",
        threshold=config.DEAD_LETTER_MAX_ATTEMPTS,
    )
    assert dead_lettered is True
    entry = registry_repo.get_entry(entry_id)
    assert entry.enabled is False

    dl = workers_repo.get_open_dead_letter("company_registry", entry_id)
    assert dl is not None
    assert dl["attempt_count"] == 8
    assert "404" in dl["last_error"]


def test_dead_lettered_item_never_claimed_again(tmp_env):
    from app.workers.leasing import claim_poll_batch

    entry_id = registry_repo.insert_entry(CompanyRegistryEntry(company_name="Acme", provider="greenhouse", tenant_identifier="acme"))
    dead_letter.record_permanent_failure(
        portal_type="company_registry", portal_id=entry_id, provider="greenhouse",
        consecutive_permanent_failures=8, last_error="404", last_attempt_id="a1", threshold=8,
    )
    claimed = claim_poll_batch(worker_id="w1", limit=10, lease_seconds=60)
    assert claimed == []


def test_requeue_reenables_and_resets_counters(tmp_env):
    entry_id = registry_repo.insert_entry(CompanyRegistryEntry(company_name="Acme", provider="greenhouse", tenant_identifier="acme"))
    dead_letter.record_permanent_failure(
        portal_type="company_registry", portal_id=entry_id, provider="greenhouse",
        consecutive_permanent_failures=8, last_error="404", last_attempt_id="a1", threshold=8,
    )
    dl = workers_repo.get_open_dead_letter("company_registry", entry_id)
    ok = dead_letter.requeue(dl["id"])
    assert ok is True

    entry = registry_repo.get_entry(entry_id)
    assert entry.enabled is True
    assert entry.consecutive_failures == 0
    assert entry.consecutive_permanent_failures == 0
    assert workers_repo.get_open_dead_letter("company_registry", entry_id) is None


def test_requeue_unknown_id_returns_false(tmp_env):
    assert dead_letter.requeue(999999) is False


def test_requeue_already_resolved_returns_false(tmp_env):
    entry_id = registry_repo.insert_entry(CompanyRegistryEntry(company_name="Acme", provider="greenhouse", tenant_identifier="acme"))
    dead_letter.record_permanent_failure(
        portal_type="company_registry", portal_id=entry_id, provider="greenhouse",
        consecutive_permanent_failures=8, last_error="404", last_attempt_id="a1", threshold=8,
    )
    dl = workers_repo.get_open_dead_letter("company_registry", entry_id)
    dead_letter.requeue(dl["id"])
    assert dead_letter.requeue(dl["id"]) is False  # already resolved
