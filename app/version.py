"""Software/capability version identifiers (CLAUDE.md Phase 6 section 19):
"Distributed deployments may briefly run mixed versions. Record: worker
software version, schema version, capability version." Bump
WORKER_SOFTWARE_VERSION when worker/runner behavior changes in a way that
matters for fleet compatibility (not for every commit)."""

import hashlib

WORKER_SOFTWARE_VERSION = "6.0.0"

# CLAUDE.md Phase 15 sections 17-18: the release-candidate application
# version. This is a plain, deterministic source-controlled string -- not a
# git tag/release (Phase 15 explicitly forbids creating those) and not
# derived from any timestamp, so /version returns the identical value on
# every process started from the same checkout. Bump this by hand when a
# release-candidate-worthy change lands, same convention as
# WORKER_SOFTWARE_VERSION above.
APP_VERSION = "15.0.0-rc1"


def release_info() -> dict:
    """Aggregates every version/capability identifier this project already
    tracks (CLAUDE.md Phase 15 section 17: "application version, schema
    version, optimizer version, classifier version, provider capability
    version") into one safe, no-secrets dict. Reused by both the /version
    endpoint and app.doctor / scripts/release_acceptance.sh so there is
    exactly one place that assembles this, never a second hand-maintained
    copy."""
    from app.migrations import CURRENT_SCHEMA_VERSION
    from app.resume_optimizer.fingerprint import OPTIMIZER_VERSION
    from app.sponsorship.classifier import CLASSIFIER_VERSION

    return {
        "app_version": APP_VERSION,
        "worker_software_version": WORKER_SOFTWARE_VERSION,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "resume_optimizer_version": OPTIMIZER_VERSION,
        "sponsorship_classifier_version": CLASSIFIER_VERSION,
        "provider_capability_fingerprint": capability_fingerprint(),
    }


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
