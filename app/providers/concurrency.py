"""Bounded concurrency helper for providers that fan out across many tenants
(e.g. many Greenhouse boards). Keeps a hard cap on simultaneous in-flight
requests so discovery never hammers upstream ATS endpoints, regardless of how
many tenants are configured -- this is the mechanism Phase 4 scales up
(more tenants, same bounded concurrency) rather than something to redesign."""

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, TypeVar

T = TypeVar("T")


def run_bounded(tasks: list[Callable[[], T]], limit: int) -> list[T]:
    """Runs each zero-arg callable in `tasks`, at most `limit` concurrently.
    Preserves input order in the returned results. A task that raises
    propagates its exception to the caller when its result is consumed --
    callers should wrap individual tasks in their own try/except if a single
    tenant failure must not abort the batch."""
    if not tasks:
        return []
    limit = max(1, limit)
    with ThreadPoolExecutor(max_workers=min(limit, len(tasks))) as pool:
        futures = [pool.submit(task) for task in tasks]
        return [f.result() for f in futures]
