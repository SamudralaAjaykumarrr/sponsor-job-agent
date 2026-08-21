"""Portal lifecycle transitions driven by verification outcomes: promotion
toward VERIFIED, demotion toward STALE/QUARANTINED after enough *permanent*
evidence (never on a single transient blip), bounded health-event history,
and deterministic ATS-migration detection. See CLAUDE.md Phase 4 sections
11, 17, 18, 24."""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from app import config
from app.registry import store
from app.registry.models import CareerPortal, PortalStatus, VerificationResult
from app.registry.verification import VerificationOutcome

logger = logging.getLogger("registry.lifecycle")

_MAX_HEALTH_EVENTS_PER_PORTAL = 50


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_health_event(
    portal_id: int, *, event_type: str, http_status: Optional[int] = None,
    error_type: str = "", latency_ms: Optional[float] = None, jobs_yield: Optional[int] = None,
    detail: str = "",
) -> None:
    from app.db import db_session

    with db_session() as conn:
        conn.execute(
            """INSERT INTO registry_portal_health_events
                 (portal_id, occurred_at, event_type, http_status, error_type, latency_ms, jobs_yield, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (portal_id, utcnow(), event_type, http_status, error_type, latency_ms, jobs_yield, detail),
        )
        # Bound history per portal so this never grows unbounded at registry scale.
        conn.execute(
            """DELETE FROM registry_portal_health_events WHERE portal_id = ? AND id NOT IN (
                 SELECT id FROM registry_portal_health_events WHERE portal_id = ?
                 ORDER BY occurred_at DESC LIMIT ?
               )""",
            (portal_id, portal_id, _MAX_HEALTH_EVENTS_PER_PORTAL),
        )


def apply_verification_outcome(portal_id: int, outcome: VerificationOutcome) -> CareerPortal:
    """The single place portal.verification_status changes in response to the
    verification pipeline. Every transition here is justified by concrete
    evidence in `outcome` -- never a bare status flip."""
    portal = store.get_portal(portal_id)
    if portal is None:
        raise ValueError(f"no such portal id={portal_id}")

    now = utcnow()
    fields: dict = {"last_verified_at": now, "notes": outcome.detail}

    if outcome.result == VerificationResult.VERIFIED:
        fields["consecutive_permanent_failures"] = 0
        fields["last_success_at"] = now
        fields["current_job_count"] = outcome.jobs_seen
        fields["identity_status"] = outcome.identity_status.value
        if portal.verification_status in (PortalStatus.DISCOVERED, PortalStatus.CANDIDATE, PortalStatus.STALE):
            fields["verification_status"] = PortalStatus.VERIFIED.value
        record_health_event(portal_id, event_type="verification_success", jobs_yield=outcome.jobs_seen, detail=outcome.detail)

    elif outcome.result == VerificationResult.AMBIGUOUS:
        fields["verification_status"] = PortalStatus.QUARANTINED.value
        fields["identity_status"] = outcome.identity_status.value
        record_health_event(portal_id, event_type="identity_mismatch", detail=outcome.detail)

    elif outcome.result == VerificationResult.UNSUPPORTED:
        # No working discovery implementation -- leave status as-is (never
        # promote, never demote for something the code simply cannot check).
        record_health_event(portal_id, event_type="verification_unsupported", detail=outcome.detail)

    elif outcome.result == VerificationResult.FAILED:
        permanent = portal.consecutive_permanent_failures + 1
        fields["consecutive_permanent_failures"] = permanent
        fields["last_failure_at"] = now
        record_health_event(portal_id, event_type="verification_permanent_failure", detail=outcome.detail)
        threshold = config.REGISTRY_STALE_AFTER_PERMANENT_FAILURES
        if permanent >= threshold:
            if portal.verification_status in (PortalStatus.VERIFIED, PortalStatus.ACTIVE):
                fields["verification_status"] = PortalStatus.STALE.value
            elif portal.verification_status in (PortalStatus.DISCOVERED, PortalStatus.CANDIDATE):
                fields["verification_status"] = PortalStatus.QUARANTINED.value

    else:  # TEMPORARY_FAILURE -- never counted toward permanent-failure demotion
        fields["last_failure_at"] = now
        record_health_event(portal_id, event_type="verification_temporary_failure", detail=outcome.detail)

    store.update_portal(portal_id, **fields)
    return store.get_portal(portal_id)


def maybe_detect_migration(company_id: int, new_portal: CareerPortal) -> Optional[dict]:
    """A migration is only inferred when the company already has a STALE
    portal on a DIFFERENT provider and the new portal has independently
    reached VERIFIED/ACTIVE -- i.e. real evidence the old one broke AND the
    new one works, not merely "a different provider showed up" (which is the
    legitimate two-portals-per-company case, see CLAUDE.md scenario E)."""
    if new_portal.verification_status not in (PortalStatus.VERIFIED, PortalStatus.ACTIVE):
        return None

    stale_candidates = [
        p for p in store.list_portals_for_company(company_id)
        if p.verification_status == PortalStatus.STALE and p.provider != new_portal.provider
        and p.id != new_portal.id
    ]
    if not stale_candidates:
        return None

    old_portal = stale_candidates[0]
    evidence = (
        f"old portal ({old_portal.provider}/{old_portal.tenant_identifier}) is STALE "
        f"after {old_portal.consecutive_permanent_failures} consecutive permanent failures; "
        f"new portal ({new_portal.provider}/{new_portal.tenant_identifier}) independently verified"
    )
    from app.db import db_session

    with db_session() as conn:
        cur = conn.execute(
            """INSERT INTO registry_migrations (company_id, old_portal_id, new_portal_id, detected_at, evidence, confidence)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (company_id, old_portal.id, new_portal.id, utcnow(), evidence, 80),
        )
        migration_id = cur.lastrowid
    store.update_portal(old_portal.id, superseded_by_portal_id=new_portal.id)
    logger.info("ATS migration detected for company_id=%s: %s", company_id, evidence)
    return {"id": migration_id, "company_id": company_id, "old_portal_id": old_portal.id,
            "new_portal_id": new_portal.id, "evidence": evidence}


def list_migrations_for_company(company_id: int) -> list[dict]:
    from app.db import db_session

    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM registry_migrations WHERE company_id = ? ORDER BY detected_at DESC", (company_id,)
        ).fetchall()
        return [dict(r) for r in rows]
