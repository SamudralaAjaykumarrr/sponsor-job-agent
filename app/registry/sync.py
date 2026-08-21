"""Bridges the Phase 4 acquisition/verification registry (registry_portals)
into the existing, unchanged Phase 3 operational polling table
(company_registry, app/registry/repo.py) that app/agent/cycle.py actually
polls. This is the ONLY integration point between the two layers -- nothing
else in Phase 3's discovery cycle, scheduler, or dashboard needs to change.

Only VERIFIED/ACTIVE portals are ever mirrored in as enabled+pollable. A
portal that regresses to STALE/QUARANTINED/DISABLED gets its mirrored row
disabled (never deleted), preserving poll history and provenance."""

import logging
from datetime import datetime, timezone

from app.registry import store
from app.registry.models import CareerPortal, PortalStatus
from app.registry.repo import get_entry, get_entry_by_tenant, insert_entry, update_entry
from app.registry.models import CompanyRegistryEntry

logger = logging.getLogger("registry.sync")

_POLLABLE_STATES = (PortalStatus.VERIFIED, PortalStatus.ACTIVE)
_DISABLED_STATES = (PortalStatus.STALE, PortalStatus.QUARANTINED, PortalStatus.DISABLED, PortalStatus.DEGRADED)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def sync_portal_to_operational_registry(portal_id: int) -> CareerPortal:
    """Idempotent: safe to call repeatedly (e.g. after every verification run
    or lifecycle transition). Mirrors a VERIFIED/ACTIVE portal into
    company_registry (creating or updating by the existing provider+tenant
    unique index) and promotes it to ACTIVE; disables the mirror (without
    deleting it) once the portal is no longer in a pollable state."""
    portal = store.get_portal(portal_id)
    if portal is None:
        raise ValueError(f"no such portal id={portal_id}")
    company = store.get_company(portal.company_id)
    if company is None:
        raise ValueError(f"portal {portal_id} references missing company_id={portal.company_id}")

    if portal.verification_status in _POLLABLE_STATES and portal.tenant_identifier and portal.enabled:
        existing = get_entry(portal.registry_entry_id) if portal.registry_entry_id else None
        if existing is None:
            existing = get_entry_by_tenant(portal.provider, portal.tenant_identifier)
        if existing is None:
            entry_id = insert_entry(CompanyRegistryEntry(
                company_name=company.display_name,
                company_domain=company.primary_domain,
                provider=portal.provider,
                tenant_identifier=portal.tenant_identifier,
                careers_url=portal.careers_url,
                country=company.country,
                support_level=portal.support_level,
                enabled=True,
                verified_at=utcnow(),
            ))
        else:
            entry_id = existing.id
            update_entry(entry_id, enabled=True, careers_url=portal.careers_url,
                          support_level=portal.support_level, verified_at=utcnow())
        store.update_portal(portal_id, registry_entry_id=entry_id, verification_status=PortalStatus.ACTIVE.value)
        logger.info("synced portal %s (%s/%s) -> company_registry id=%s (ACTIVE)",
                    portal_id, portal.provider, portal.tenant_identifier, entry_id)

    elif portal.verification_status in _DISABLED_STATES or not portal.enabled:
        if portal.registry_entry_id:
            update_entry(portal.registry_entry_id, enabled=False)
            logger.info("disabled operational mirror company_registry id=%s for portal %s (status=%s)",
                        portal.registry_entry_id, portal_id, portal.verification_status.value)

    return store.get_portal(portal_id)
