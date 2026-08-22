"""Work-queue abstraction (CLAUDE.md Phase 5 section 5/33): the worker
runner and the rest of the pipeline talk to `WorkQueue`, never to SQLite
locking details directly. `SQLitePollQueue`/`SQLiteVerificationQueue` are the
only implementation today; a future PostgreSQL/Redis/SQS-backed queue would
implement the same four methods (claim_due_work/ack/retry/fail/extend_lease)
without any caller code changing. See docs/worker-architecture.md's "Future
queue backend" section for exactly what that swap would involve."""

from abc import ABC, abstractmethod

from app.workers import leasing
from app.workers.models import LeasedWorkItem, PortalType


class WorkQueue(ABC):
    @abstractmethod
    def claim_due_work(self, *, worker_id: str, limit: int, lease_seconds: int,
                        shard_count: int = 1, shard_index: int = 0) -> list[LeasedWorkItem]:
        """Atomically claims up to `limit` due work items for this worker."""

    @abstractmethod
    def ack(self, item: LeasedWorkItem) -> None:
        """Releases the lease after successful completion."""

    @abstractmethod
    def retry(self, item: LeasedWorkItem) -> None:
        """Releases the lease after a retryable failure -- the scheduling of
        *when* it becomes due again is the caller's responsibility (it
        already updated next_poll_at/next_retry_at); this just frees the
        lease immediately rather than waiting for expiry."""

    @abstractmethod
    def fail(self, item: LeasedWorkItem) -> None:
        """Releases the lease after a permanent failure."""

    @abstractmethod
    def extend_lease(self, item: LeasedWorkItem, *, lease_seconds: int) -> bool:
        """Heartbeat/renewal for a long-running attempt. Returns False if the
        lease was already lost."""


class SQLitePollQueue(WorkQueue):
    """The operational job-discovery poll queue -- one item per
    company_registry (ACTIVE portal) row."""

    def claim_due_work(self, *, worker_id: str, limit: int, lease_seconds: int,
                        shard_count: int = 1, shard_index: int = 0) -> list[LeasedWorkItem]:
        rows = leasing.claim_poll_batch(
            worker_id=worker_id, limit=limit, lease_seconds=lease_seconds,
            shard_count=shard_count, shard_index=shard_index,
        )
        return [
            LeasedWorkItem(
                portal_type=PortalType.COMPANY_REGISTRY, portal_id=r["id"], provider=r["provider"],
                tenant_identifier=r["tenant_identifier"], attempt_id=r["lease_attempt_id"],
                company_name=r["company_name"], registry_id=r["id"],
            )
            for r in rows
        ]

    def ack(self, item: LeasedWorkItem) -> None:
        leasing.release_poll_lease(item.portal_id, expected_attempt_id=item.attempt_id)

    def retry(self, item: LeasedWorkItem) -> None:
        leasing.release_poll_lease(item.portal_id, expected_attempt_id=item.attempt_id)

    def fail(self, item: LeasedWorkItem) -> None:
        leasing.release_poll_lease(item.portal_id, expected_attempt_id=item.attempt_id)

    def extend_lease(self, item: LeasedWorkItem, *, lease_seconds: int) -> bool:
        return leasing.extend_poll_lease(item.portal_id, item.attempt_id, lease_seconds=lease_seconds)


class SQLiteVerificationQueue(WorkQueue):
    """The portal-verification queue -- one item per registry_portals row
    still in DISCOVERED/CANDIDATE status."""

    def claim_due_work(self, *, worker_id: str, limit: int, lease_seconds: int,
                        shard_count: int = 1, shard_index: int = 0) -> list[LeasedWorkItem]:
        rows = leasing.claim_verification_batch(
            worker_id=worker_id, limit=limit, lease_seconds=lease_seconds,
            shard_count=shard_count, shard_index=shard_index,
        )
        return [
            LeasedWorkItem(
                portal_type=PortalType.REGISTRY_PORTAL, portal_id=r["id"], provider=r["provider"] or "",
                tenant_identifier=r["tenant_identifier"] or "", attempt_id=r["verify_lease_attempt_id"],
                registry_id=r.get("registry_entry_id"),
            )
            for r in rows
        ]

    def ack(self, item: LeasedWorkItem) -> None:
        leasing.release_verification_lease(item.portal_id, expected_attempt_id=item.attempt_id)

    def retry(self, item: LeasedWorkItem) -> None:
        leasing.release_verification_lease(item.portal_id, expected_attempt_id=item.attempt_id)

    def fail(self, item: LeasedWorkItem) -> None:
        leasing.release_verification_lease(item.portal_id, expected_attempt_id=item.attempt_id)

    def extend_lease(self, item: LeasedWorkItem, *, lease_seconds: int) -> bool:
        return leasing.extend_verification_lease(item.portal_id, item.attempt_id, lease_seconds=lease_seconds)
