"""Worker capability model (CLAUDE.md Phase 8 section 39). A worker process
declares which queues it's willing to claim work from via
`workers.capabilities` (JSON list, see app.workers.repo.upsert_worker). A
plain Phase 5-7 discovery worker never sets this column, so it can never
accidentally claim application-executor work -- capability-gating is
opt-in, not opt-out."""

import json
from enum import Enum


class WorkerCapability(str, Enum):
    DISCOVERY = "DISCOVERY"
    REGISTRY_VERIFY = "REGISTRY_VERIFY"
    APPLICATION_PREPARE = "APPLICATION_PREPARE"
    APPLICATION_SUBMIT = "APPLICATION_SUBMIT"


def encode(capabilities: list[WorkerCapability]) -> str:
    return json.dumps([c.value for c in capabilities])


def decode(capabilities_json: str) -> set[str]:
    try:
        return set(json.loads(capabilities_json or "[]"))
    except (ValueError, TypeError):
        return set()


def has_capability(capabilities_json: str, capability: WorkerCapability) -> bool:
    return capability.value in decode(capabilities_json)
