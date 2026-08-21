"""Sanity-checks that claim queries stay bounded regardless of how many due
rows exist -- the real scale benchmark (1k/10k/50k/100k, which would slow
pytest) lives in scripts/worker_benchmark.py per CLAUDE.md Phase 5 section
38; this just proves correctness of the LIMIT behavior at a size pytest can
afford."""

import time

from app.registry.models import CompanyRegistryEntry
from app.registry import repo as registry_repo
from app.workers.leasing import claim_poll_batch


def test_claim_never_exceeds_limit_even_with_many_more_due(tmp_env):
    for i in range(2000):
        registry_repo.insert_entry(CompanyRegistryEntry(company_name=f"C{i}", provider="greenhouse", tenant_identifier=f"t{i}"))

    claimed = claim_poll_batch(worker_id="w1", limit=25, lease_seconds=60)
    assert len(claimed) == 25


def test_claim_query_time_does_not_blow_up_with_table_size(tmp_env):
    for i in range(3000):
        registry_repo.insert_entry(CompanyRegistryEntry(company_name=f"C{i}", provider="greenhouse", tenant_identifier=f"t{i}"))

    start = time.monotonic()
    claim_poll_batch(worker_id="w1", limit=50, lease_seconds=60)
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, f"claim took {elapsed:.3f}s against an indexed 3000-row table -- expected well under 1s"
