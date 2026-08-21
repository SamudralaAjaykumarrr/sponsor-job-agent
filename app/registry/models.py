from datetime import datetime, timezone
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
