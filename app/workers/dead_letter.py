"""Dead-letter/quarantine for work items that fail permanently and
repeatedly, so nothing retries forever (CLAUDE.md Phase 5 section 25).

Distinct from app.registry.lifecycle's STALE/QUARANTINED portal-status
demotion (Phase 4): that mechanism changes a portal's *lifecycle* state
(whether it should ever be verified/polled again) using its own threshold
(REGISTRY_STALE_AFTER_PERMANENT_FAILURES). This module is the *execution*
layer's bookkeeping on top of that -- it also disables the operational
company_registry row (so claim_poll_batch stops selecting it at all) and
gives operators one unified place (`python -m app.workers.cli dead-letter`)
to see every work item that gave up, from either queue, with the option to
safely requeue after a fix."""

from app.db import db_session
from app.workers import repo as workers_repo


def record_permanent_failure(
    *, portal_type: str, portal_id: int, provider: str, consecutive_permanent_failures: int,
    last_error: str, last_attempt_id: str, threshold: int,
) -> bool:
    """Call after every PERMANENT_FAILURE attempt. If the portal has now hit
    `threshold` consecutive permanent failures, disables it (so it's never
    claimed again) and records/updates its dead-letter entry. Returns True
    if the item was just dead-lettered."""
    if consecutive_permanent_failures < threshold:
        return False

    workers_repo.upsert_dead_letter(
        portal_type=portal_type, portal_id=portal_id, provider=provider,
        reason=f"{consecutive_permanent_failures} consecutive permanent failures (threshold={threshold})",
        attempt_count=consecutive_permanent_failures, last_error=last_error, last_attempt_id=last_attempt_id,
    )
    _disable(portal_type, portal_id)
    return True


def _disable(portal_type: str, portal_id: int) -> None:
    table = "company_registry" if portal_type == "company_registry" else "registry_portals"
    with db_session() as conn:
        conn.execute(f"UPDATE {table} SET enabled = 0 WHERE id = ?", (portal_id,))


def _reset_failure_counters(portal_type: str, portal_id: int) -> None:
    if portal_type == "company_registry":
        with db_session() as conn:
            conn.execute(
                "UPDATE company_registry SET enabled = 1, consecutive_failures = 0, "
                "consecutive_permanent_failures = 0 WHERE id = ?",
                (portal_id,),
            )
    else:
        with db_session() as conn:
            conn.execute(
                "UPDATE registry_portals SET enabled = 1, consecutive_failures = 0, "
                "consecutive_permanent_failures = 0 WHERE id = ?",
                (portal_id,),
            )


def requeue(dead_letter_id: int) -> bool:
    """Operator action after fixing the underlying problem: re-enables the
    work item, resets its failure counters, and marks the dead-letter row
    resolved. Never automatic -- always an explicit human action."""
    with db_session() as conn:
        row = conn.execute("SELECT * FROM dead_letters WHERE id = ? AND resolved = 0", (dead_letter_id,)).fetchone()
    if row is None:
        return False
    _reset_failure_counters(row["portal_type"], row["portal_id"])
    workers_repo.resolve_dead_letter(dead_letter_id)
    return True


def list_dead_letters(limit: int = 200) -> list[dict]:
    return workers_repo.list_dead_letters(resolved=False, limit=limit)
