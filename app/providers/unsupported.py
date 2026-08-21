"""Providers with no verified, safe, reliable, unauthenticated public
discovery interface. Each still exists as a real JobProvider so it appears
uniformly in the provider registry/capabilities/dashboard, but fetch_jobs()
never fabricates results -- it logs once and returns an empty list. See
docs/provider-capabilities.md for exactly why each one is UNSUPPORTED and
what a future, verified implementation would need."""

import logging
from typing import Optional

import httpx

from app.providers.base import JobProvider, RawJobPosting
from app.providers.capabilities import ProviderCapabilities, SupportLevel

logger = logging.getLogger("providers.unsupported")


class _UnsupportedProvider(JobProvider):
    """fetch_jobs always returns [] -- never crashes a discovery cycle, never
    invents a job. tenant_identifiers accepted for interface symmetry with
    real providers but unused."""

    def __init__(self, tenant_identifiers: Optional[list[str]] = None,
                 client: Optional[httpx.Client] = None, timeout: float = 10.0):
        self.tenant_identifiers = tenant_identifiers or []
        self._warned = False

    def fetch_jobs(self, max_jobs: int) -> list[RawJobPosting]:
        if not self._warned:
            logger.warning(
                "%s: discovery not implemented (%s) -- returning no jobs",
                self.name, self.capabilities.notes,
            )
            self._warned = True
        return []


class TeamtailorProvider(_UnsupportedProvider):
    name = "teamtailor"
    capabilities = ProviderCapabilities(
        provider_name="teamtailor",
        provider_version="0.0.0",
        discovery_supported=False,
        detail_fetch_supported=False,
        structured_location_supported=False,
        structured_published_at_supported=False,
        structured_salary_supported=False,
        structured_employment_type_supported=False,
        public_interface=False,
        requires_credentials=True,
        submission_supported=False,
        support_level=SupportLevel.UNSUPPORTED,
        notes="Teamtailor's documented Careers API requires a partner API key; no verified stable "
              "unauthenticated public JSON endpoint was found. Detection/registry only.",
    )


class JobviteProvider(_UnsupportedProvider):
    name = "jobvite"
    capabilities = ProviderCapabilities(
        provider_name="jobvite",
        provider_version="0.0.0",
        discovery_supported=False,
        detail_fetch_supported=False,
        structured_location_supported=False,
        structured_published_at_supported=False,
        structured_salary_supported=False,
        structured_employment_type_supported=False,
        public_interface=False,
        requires_credentials=False,
        submission_supported=False,
        support_level=SupportLevel.UNSUPPORTED,
        notes="No verified stable unauthenticated public JSON discovery endpoint found across tenants. "
              "Detection/registry only.",
    )


class PinpointProvider(_UnsupportedProvider):
    name = "pinpoint"
    capabilities = ProviderCapabilities(
        provider_name="pinpoint",
        provider_version="0.0.0",
        discovery_supported=False,
        detail_fetch_supported=False,
        structured_location_supported=False,
        structured_published_at_supported=False,
        structured_salary_supported=False,
        structured_employment_type_supported=False,
        public_interface=False,
        requires_credentials=False,
        submission_supported=False,
        support_level=SupportLevel.UNSUPPORTED,
        notes="No verified stable unauthenticated public JSON discovery endpoint found. Detection/registry only.",
    )


class JazzHRProvider(_UnsupportedProvider):
    name = "jazzhr"
    capabilities = ProviderCapabilities(
        provider_name="jazzhr",
        provider_version="0.0.0",
        discovery_supported=False,
        detail_fetch_supported=False,
        structured_location_supported=False,
        structured_published_at_supported=False,
        structured_salary_supported=False,
        structured_employment_type_supported=False,
        public_interface=False,
        requires_credentials=True,
        submission_supported=False,
        support_level=SupportLevel.UNSUPPORTED,
        notes="JazzHR's public API requires an API key; no verified unauthenticated discovery endpoint. "
              "Detection/registry only.",
    )


class ICIMSProvider(_UnsupportedProvider):
    name = "icims"
    capabilities = ProviderCapabilities(
        provider_name="icims",
        provider_version="0.0.0",
        discovery_supported=False,
        detail_fetch_supported=False,
        structured_location_supported=False,
        structured_published_at_supported=False,
        structured_salary_supported=False,
        structured_employment_type_supported=False,
        public_interface=False,
        requires_credentials=False,
        submission_supported=False,
        support_level=SupportLevel.UNSUPPORTED,
        notes="iCIMS career-site search endpoints vary heavily per tenant and commonly require "
              "session cookies/CSRF tokens issued by the search page itself, which this application "
              "will not fabricate/replay. Detection/registry only; do not claim universal support.",
    )


class OracleRecruitingProvider(_UnsupportedProvider):
    name = "oracle"
    capabilities = ProviderCapabilities(
        provider_name="oracle",
        provider_version="0.0.0",
        discovery_supported=False,
        detail_fetch_supported=False,
        structured_location_supported=False,
        structured_published_at_supported=False,
        structured_salary_supported=False,
        structured_employment_type_supported=False,
        public_interface=False,
        requires_credentials=False,
        submission_supported=False,
        support_level=SupportLevel.UNSUPPORTED,
        notes="Oracle Recruiting Cloud / Oracle Cloud Careers site parameters (site id, locale, finder) "
              "vary per tenant with no reliably guessable pattern. Detection/registry only.",
    )
