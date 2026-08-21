from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from app.providers.capabilities import SupportLevel


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class CompanyRegistryEntry(BaseModel):
    """One company/tenant mapping to an ATS provider. Foundation for the
    Phase 4 mass importer (10k-100k+ rows) -- SQLite-backed so scale is a
    matter of loading more rows, not changing code."""

    id: Optional[int] = None
    company_name: str
    company_domain: str = ""
    provider: str
    tenant_identifier: str
    careers_url: str = ""
    country: str = ""
    enabled: bool = True

    verified_at: Optional[str] = None
    last_success_at: Optional[str] = None
    last_failure_at: Optional[str] = None
    last_error: str = ""
    consecutive_failures: int = 0
    support_level: SupportLevel = SupportLevel.FULL
    notes: str = ""

    last_polled_at: Optional[str] = None
    next_poll_at: Optional[str] = None
    average_job_yield: float = 0.0
    average_latency_ms: float = 0.0
    poll_interval_minutes: int = 15

    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)


# --- Phase 4: acquisition/verification/lifecycle registry -------------------
# Additive layer on top of CompanyRegistryEntry above (which remains the
# unchanged Phase 3 operational polling table). See docs/phase4-company-registry.md.


class PortalStatus(str, Enum):
    """Full lifecycle of a candidate career portal. Only VERIFIED/ACTIVE rows
    are ever mirrored into the operational company_registry table for polling
    (app/registry/sync.py) -- never inflate a row to VERIFIED/ACTIVE without
    the verification pipeline actually confirming it."""

    DISCOVERED = "DISCOVERED"
    CANDIDATE = "CANDIDATE"
    VERIFIED = "VERIFIED"
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    QUARANTINED = "QUARANTINED"
    DISABLED = "DISABLED"


class DiscoveryStatus(str, Enum):
    """How a portal candidate was found -- independent of its lifecycle state."""

    IMPORTED = "IMPORTED"
    DETECTED = "DETECTED"
    PAGE_DISCOVERY = "PAGE_DISCOVERY"
    MANUAL = "MANUAL"


class VerificationResult(str, Enum):
    """Outcome of one run of the verification pipeline (app/registry/verification.py)."""

    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    AMBIGUOUS = "AMBIGUOUS"
    UNSUPPORTED = "UNSUPPORTED"
    TEMPORARY_FAILURE = "TEMPORARY_FAILURE"


class IdentityStatus(str, Enum):
    UNKNOWN = "UNKNOWN"
    MATCHED = "MATCHED"
    MISMATCH = "MISMATCH"


class Company(BaseModel):
    id: Optional[int] = None
    normalized_name: str
    display_name: str
    primary_domain: str = ""
    careers_home_url: str = ""
    country: str = ""
    headquarters_location: str = ""
    enabled: bool = True
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)


class CareerPortal(BaseModel):
    id: Optional[int] = None
    company_id: int
    # "" means "provider not yet known/detected" -- a careers-URL-only
    # DISCOVERED row is legitimate (see importers.py); never guess a provider.
    provider: str = ""
    tenant_identifier: str = ""
    careers_url: str = ""
    jobs_url: str = ""
    canonical_url: str = ""
    support_level: SupportLevel = SupportLevel.UNSUPPORTED
    discovery_status: DiscoveryStatus = DiscoveryStatus.IMPORTED
    verification_status: PortalStatus = PortalStatus.DISCOVERED
    identity_status: IdentityStatus = IdentityStatus.UNKNOWN
    enabled: bool = True
    confidence: int = 0
    confidence_reasons: list[str] = Field(default_factory=list)

    last_verified_at: Optional[str] = None
    last_polled_at: Optional[str] = None
    next_poll_at: Optional[str] = None
    last_success_at: Optional[str] = None
    last_failure_at: Optional[str] = None
    consecutive_failures: int = 0
    consecutive_permanent_failures: int = 0
    average_job_yield: float = 0.0
    average_latency_ms: float = 0.0
    current_job_count: int = 0
    poll_interval_minutes: int = 15

    registry_entry_id: Optional[int] = None
    superseded_by_portal_id: Optional[int] = None
    notes: str = ""

    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)


class RegistryProvenance(BaseModel):
    id: Optional[int] = None
    portal_id: Optional[int] = None
    company_id: Optional[int] = None
    source_type: str
    source_name: str = ""
    source_url: str = ""
    imported_at: str = Field(default_factory=utcnow)
    observed_at: str = Field(default_factory=utcnow)
    evidence: str = ""
    confidence: int = 0
    created_at: str = Field(default_factory=utcnow)
