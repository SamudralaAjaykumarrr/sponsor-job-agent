"""Software/capability version identifiers (CLAUDE.md Phase 6 section 19):
"Distributed deployments may briefly run mixed versions. Record: worker
software version, schema version, capability version." Bump
WORKER_SOFTWARE_VERSION when worker/runner behavior changes in a way that
matters for fleet compatibility (not for every commit)."""

import hashlib

WORKER_SOFTWARE_VERSION = "6.0.0"


def capability_fingerprint() -> str:
    """Short, deterministic hash of every known provider's name + support
    level. Changes whenever a provider's capability declaration changes
    (e.g. a provider promoted from PARTIAL to FULL, or a new provider
    added) -- lets a dashboard/operator notice a worker running with an
    older provider-capability set than the rest of the fleet."""
    from app.providers.registry import all_capabilities

    parts = sorted(f"{c.provider_name}:{c.support_level.value}:{c.provider_version}" for c in all_capabilities())
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:12]
