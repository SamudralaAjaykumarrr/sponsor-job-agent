from dataclasses import dataclass
from enum import Enum


class SupportLevel(str, Enum):
    FULL = "FULL"                # discovery implemented, tested against real fixtures, expected to work broadly
    PARTIAL = "PARTIAL"          # discovery implemented but only for a subset of tenants/configs, or missing fields
    EXPERIMENTAL = "EXPERIMENTAL"  # implemented against a best-effort/unofficial pattern; may break without notice
    UNSUPPORTED = "UNSUPPORTED"  # no safe/reliable public interface found; detection/registry only


@dataclass(frozen=True)
class ProviderCapabilities:
    """Machine-readable description of what a provider connector actually does.
    This is the single source of truth the dashboard, docs, and tests read from
    -- never hand-wave a capability that isn't backed by working code."""

    provider_name: str
    provider_version: str
    discovery_supported: bool
    detail_fetch_supported: bool
    structured_location_supported: bool
    structured_published_at_supported: bool
    structured_salary_supported: bool
    structured_employment_type_supported: bool
    public_interface: bool
    requires_credentials: bool
    submission_supported: bool
    support_level: SupportLevel
    notes: str = ""

    def as_dict(self) -> dict:
        return {
            "provider_name": self.provider_name,
            "provider_version": self.provider_version,
            "discovery_supported": self.discovery_supported,
            "detail_fetch_supported": self.detail_fetch_supported,
            "structured_location_supported": self.structured_location_supported,
            "structured_published_at_supported": self.structured_published_at_supported,
            "structured_salary_supported": self.structured_salary_supported,
            "structured_employment_type_supported": self.structured_employment_type_supported,
            "public_interface": self.public_interface,
            "requires_credentials": self.requires_credentials,
            "submission_supported": self.submission_supported,
            "support_level": self.support_level.value,
            "notes": self.notes,
        }
