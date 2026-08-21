"""Bounded live verification pipeline for candidate career portals.

A portal only becomes VERIFIED after real evidence: the provider is
recognized and actually implemented, a tenant identifier was deterministically
extracted (never guessed), and a real bounded request to the provider's own
public endpoint succeeds. Distinguishes a permanent structural problem (bad
tenant, 404) from a transient one (timeout/429/5xx) so one bad network moment
never permanently discards a portal -- see CLAUDE.md Phase 4 section 11.

Two-step design:
  1. A raw structural probe (app/registry/probe.py) against the provider's own
     endpoint -- this is what actually determines VERIFIED/FAILED/
     TEMPORARY_FAILURE, because it raises on failure instead of swallowing it
     (unlike JobProvider.fetch_jobs(), which deliberately isolates per-tenant
     errors for the discovery cycle's sake).
  2. A best-effort call through the normal JobProvider connector to get a
     normalized job count + company-identity signal for a VERIFIED portal.
     Any failure here is informational only and never downgrades a verdict
     the probe already confirmed."""

import logging
from dataclasses import dataclass
from typing import Callable, Optional

import httpx

from app import config
from app.providers.base import JobProvider
from app.providers.capabilities import SupportLevel
from app.providers.http_client import ProviderHTTPError
from app.providers.registry import build_provider_for_tenant, get_capabilities
from app.registry import probe as probe_mod
from app.registry.models import CareerPortal, IdentityStatus, VerificationResult
from app.registry.normalize import normalize_company_name

logger = logging.getLogger("registry.verification")

_TEMPORARY_MARKERS = (
    "429", "500", "502", "503", "504", "timeout", "Timeout",
    "request failed after", "TransportError", "connection",
)
_PERMANENT_MARKERS = ("400", "401", "403", "404", "410")


@dataclass(frozen=True)
class VerificationOutcome:
    result: VerificationResult
    detail: str
    jobs_seen: int = 0
    identity_status: IdentityStatus = IdentityStatus.UNKNOWN
    is_permanent_failure: bool = False


def _classify_http_error(message: str) -> VerificationResult:
    if any(marker in message for marker in _PERMANENT_MARKERS):
        return VerificationResult.FAILED
    if any(marker in message for marker in _TEMPORARY_MARKERS):
        return VerificationResult.TEMPORARY_FAILURE
    # Unrecognized shape -- treat conservatively as temporary so a portal is
    # never permanently discarded on an ambiguous error message.
    return VerificationResult.TEMPORARY_FAILURE


def _check_identity(portal_company_name: str, observed_company_names: list[str]) -> IdentityStatus:
    if not observed_company_names:
        return IdentityStatus.UNKNOWN
    target = set(normalize_company_name(portal_company_name).split())
    if not target:
        return IdentityStatus.UNKNOWN
    for observed in observed_company_names:
        observed_tokens = set(normalize_company_name(observed).split())
        if target & observed_tokens:
            return IdentityStatus.MATCHED
    return IdentityStatus.MISMATCH


def verify_portal(
    portal: CareerPortal,
    *,
    company_display_name: str,
    client: Optional[httpx.Client] = None,
    provider_factory: Callable[[str, str], Optional[JobProvider]] = build_provider_for_tenant,
) -> VerificationOutcome:
    """Runs the structural + live checks for one candidate portal. Never
    raises -- every failure mode is captured in the returned outcome so
    callers (CLI, dashboard action, tests) can update lifecycle state
    uniformly. `client`, when given (e.g. an httpx.MockTransport-backed
    client in tests), is used for both the raw probe and the informational
    fetch."""
    caps = get_capabilities(portal.provider)
    if caps is None or caps.support_level == SupportLevel.UNSUPPORTED or not caps.discovery_supported:
        return VerificationOutcome(
            VerificationResult.UNSUPPORTED,
            detail=f"provider '{portal.provider}' has no working discovery implementation "
                    "(UNSUPPORTED support level) -- verification cannot proceed",
        )

    if not portal.tenant_identifier:
        return VerificationOutcome(
            VerificationResult.FAILED,
            detail="no tenant identifier could be deterministically extracted -- never fabricating one",
        )

    if not probe_mod.has_probe(portal.provider):
        return VerificationOutcome(
            VerificationResult.UNSUPPORTED,
            detail=f"no structural probe implemented for provider '{portal.provider}' yet",
        )

    try:
        probe_mod.probe(portal.provider, portal.tenant_identifier, client=client)
    except ProviderHTTPError as exc:
        message = str(exc)
        result = _classify_http_error(message)
        return VerificationOutcome(result, detail=message, is_permanent_failure=(result == VerificationResult.FAILED))
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        return VerificationOutcome(VerificationResult.TEMPORARY_FAILURE, detail=f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 - unexpected errors are a temporary signal, not a permanent verdict
        logger.warning("verification probe for %s/%s raised unexpectedly", portal.provider, portal.tenant_identifier, exc_info=True)
        return VerificationOutcome(VerificationResult.TEMPORARY_FAILURE, detail=f"unexpected error: {exc}")

    # Probe succeeded -- the endpoint is real and responding. Best-effort
    # enrichment via the normal connector for job count + identity signal;
    # never lets a secondary failure here override the confirmed VERIFIED verdict.
    jobs = []
    provider = provider_factory(portal.provider, portal.tenant_identifier)
    if provider is not None:
        try:
            jobs = provider.fetch_jobs(max_jobs=config.REGISTRY_VERIFICATION_PROBE_JOBS)
        except Exception:
            jobs = []

    observed_companies = [j.company for j in jobs if j.company]
    identity = _check_identity(company_display_name, observed_companies)
    if identity == IdentityStatus.MISMATCH:
        return VerificationOutcome(
            VerificationResult.AMBIGUOUS,
            detail=f"provider responded, but returned company name(s) {sorted(set(observed_companies))!r} "
                    f"do not match registry company '{company_display_name}' -- needs review before activating",
            jobs_seen=len(jobs), identity_status=identity,
        )

    return VerificationOutcome(
        VerificationResult.VERIFIED,
        detail=f"provider endpoint returned a valid response ({len(jobs)} job(s) visible)",
        jobs_seen=len(jobs),
        identity_status=identity,  # UNKNOWN when the board is currently empty -- never fabricate a match
    )
