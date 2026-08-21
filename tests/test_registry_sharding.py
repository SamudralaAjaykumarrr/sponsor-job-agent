from app.registry.sharding import in_shard, shard_for_portal


def test_shard_default_single_shard_everything_in_shard_zero():
    for portal_id in range(1, 50):
        assert shard_for_portal(portal_id, 1) == 0


def test_shard_deterministic():
    assert shard_for_portal(12345, 4) == shard_for_portal(12345, 4)
    assert shard_for_portal(1, 8) == shard_for_portal(1, 8)


def test_every_portal_assigned_exactly_one_shard():
    shard_count = 4
    for portal_id in range(1, 2001):
        matches = [idx for idx in range(shard_count) if in_shard(portal_id, shard_count, idx)]
        assert len(matches) == 1


def test_no_overlap_across_shards():
    shard_count = 4
    assignments = {idx: set() for idx in range(shard_count)}
    for portal_id in range(1, 2001):
        assignments[shard_for_portal(portal_id, shard_count)].add(portal_id)
    all_ids = set()
    for ids in assignments.values():
        assert not (all_ids & ids)
        all_ids |= ids
    assert len(all_ids) == 2000


def test_reasonable_distribution_across_shards():
    shard_count = 4
    n = 4000
    counts = [0] * shard_count
    for portal_id in range(1, n + 1):
        counts[shard_for_portal(portal_id, shard_count)] += 1
    expected = n / shard_count
    for c in counts:
        assert 0.5 * expected <= c <= 1.5 * expected
