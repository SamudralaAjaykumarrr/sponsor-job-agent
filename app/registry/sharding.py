"""Deterministic registry partitioning, prepared for a future distributed
Phase 7 -- NOT distributed infrastructure itself. Default local mode is
always 1 shard / index 0 (see REGISTRY_SHARD_COUNT/REGISTRY_SHARD_INDEX in
app/config.py), so nothing changes for the current single-process app unless
someone explicitly configures more shards."""

import hashlib


def shard_for_portal(portal_id: int, shard_count: int) -> int:
    """Pure, deterministic hash -> shard mapping. Same portal_id + shard_count
    always yields the same shard, with no dependency on wall-clock time or
    insertion order."""
    if shard_count <= 1:
        return 0
    digest = hashlib.sha256(str(portal_id).encode("utf-8")).hexdigest()
    return int(digest, 16) % shard_count


def in_shard(portal_id: int, shard_count: int, shard_index: int) -> bool:
    return shard_for_portal(portal_id, shard_count) == shard_index
