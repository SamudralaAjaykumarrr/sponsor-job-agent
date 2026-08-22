"""Typed enums/dataclasses for the Phase 5 distributed polling execution
layer. Kept separate from app.registry.models (Phase 4 lifecycle) since these
describe *execution* concepts (leases, attempts, worker status) rather than
portal lifecycle state."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkerStatus(str, Enum):
    STARTING = "STARTING"
    IDLE = "IDLE"
    WORKING = "WORKING"
    DEGRADED = "DEGRADED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    # Phase 6: assigned only by app.workers.reaper.reap_orphans() when a
    # worker's heartbeat has gone stale past a configurable threshold --
    # never assigned by the worker itself (a live worker only ever sets
    # STARTING/IDLE/WORKING/STOPPING/STOPPED on its own row).
    OFFLINE = "OFFLINE"


class Queue(str, Enum):
    POLL = "poll"
    VERIFICATION = "verification"


class PortalType(str, Enum):
    COMPANY_REGISTRY = "company_registry"
    REGISTRY_PORTAL = "registry_portal"


class AttemptStatus(str, Enum):
    LEASED = "LEASED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    CANCELLED = "CANCELLED"


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass
class LeasedWorkItem:
    """One unit of work claimed by a worker: either a company_registry row
    (poll queue) or a registry_portals row (verification queue)."""

    portal_type: PortalType
    portal_id: int
    provider: str
    tenant_identifier: str
    attempt_id: str
    # Extra context the caller needs without a second DB round trip.
    company_name: str = ""
    registry_id: Optional[int] = None  # company_registry id, for verification->sync bridging


@dataclass
class AttemptRecord:
    attempt_id: str
    portal_type: str
    portal_id: int
    worker_id: str
    provider: str
    queue: str
    started_at: str
    finished_at: Optional[str] = None
    status: str = AttemptStatus.LEASED.value
    jobs_received: int = 0
    jobs_new: int = 0
    jobs_duplicate: int = 0
    jobs_filtered: int = 0
    latency_ms: Optional[float] = None
    error_type: str = ""
    detail: str = ""
    retryable: bool = False
    next_retry_at: Optional[str] = None
    cycle_id: Optional[int] = None
