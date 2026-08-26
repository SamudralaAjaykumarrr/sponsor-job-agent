"""Workday application-form adapter (Workday + Ashby Provider Execution V1).

Live-checked 2026-08-26 against a real, public Workday tenant
(walmart.wd504.myworkdayjobs.com/WalmartExternal, the same CXS API
`app.providers.workday` already uses for discovery): the per-job detail
endpoint (`GET {tenant}.{wdhost}/wday/cxs/{tenant}/{site}{externalPath}`)
returns a `jobPostingInfo` object whose keys are exactly `canApply, country,
externalUrl, id, includeResumeParsing, jobDescription, jobPostingId,
jobPostingSiteId, jobReqId, jobRequisitionLocation, location, posted,
postedOn, questionnaireId, secondaryQuestionnaireId, startDate, timeType,
title` -- genuine structured fields, but no application-question schema of
any kind. `questionnaireId`/`secondaryQuestionnaireId` are opaque reference
ids only; no documented public endpoint exists to fetch their content
without an authenticated candidate session (Workday's real apply flow is the
separate "Candidate Home" experience, which commonly requires account
creation/login -- see `app.applications.workday_tenant`'s own genuinely
VARIABLE per-tenant login observations). API-side form discovery is
therefore honestly UNSUPPORTED here, matching
`app.applications.providers_lever`'s precedent, not guessed.

What IS genuine and structured on this same public endpoint:
  - `canApply` (bool): live-verified True on an open posting. False is real
    evidence the tenant will not currently accept an application for this
    requisition (CLOSED) -- distinct from the posting being entirely gone
    (404/410, REMOVED/EXPIRED).
  - `jobReqId` (e.g. "R-2623121"): the stable requisition id, matching
    `app.applications.workday_tenant`'s own `_REQUISITION_RE` extraction from
    the candidate-facing URL.

Canonical identity is derived from the job's own real candidate-facing URL
via `app.applications.workday_tenant.parse_workday_tenant()` -- the single,
already-tested source of tenant/site/host/requisition-id parsing for this
project -- rather than a second, parallel URL-parsing implementation.
`job.company_identifier` only stores the tenant (see `app.providers.workday`),
never the site, so the URL is the primary and most reliable identity source
here (unlike Greenhouse/Lever, where the job row's own token+id pair is
primary and the URL is only a fallback).

Submission stays NOT implemented and `submission_supported` stays False:
proving any of the ten REAL SUBMISSION CAPABILITY requirements would require
completing a real Workday tenant's actual (often account-gated) apply flow
end-to-end, which this project never does."""

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import httpx

from app.applications.models import (
    ApplicationCapabilities,
    AutomationPolicy,
    ConfirmationResult,
    DraftResult,
    FormSnapshot,
    MappingResult,
    PolicyReason,
    SubmitResult,
    SupportLevel,
    ValidationResult,
)
from app.applications.provider import ApplicationProvider
from app.applications.workday_tenant import WorkdayTenantInfo, parse_workday_tenant
from app.config import PROVIDER_HTTP_TIMEOUT_SECONDS
from app.models import Job
from app.providers.http_client import ProviderHTTPError, build_client, get_json

# Same permanent-vs-temporary split every other execution adapter in this
# project uses (CLAUDE.md's Phase 4 registry-lifecycle rule, reaffirmed by
# Greenhouse/Lever's own execution adapters). 401/403 are deliberately
# excluded -- they mean "we were refused", never "the requisition is gone".
_GONE_STATUS_CODES = frozenset({404, 410})

# The candidate URL path is `/{site}/job/{location}/{title}_{req}` (an
# optional `/{lang}-{REGION}` locale prefix may precede the site segment --
# see app.applications.workday_tenant's own `_SITE_FROM_PATH_RE`, which this
# mirrors only far enough to recover the `/job/...` suffix; the tenant/site/
# host/requisition parsing itself is never duplicated, only reused via
# `parse_workday_tenant()`).
_SITE_PREFIX_RE = re.compile(r"^/(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[^/]+)/job/", re.I)


def _external_path(url: str) -> str:
    """Recovers the `/job/{location}/{title}_{req}` suffix from a real
    Workday candidate URL -- exactly the value
    `app.providers.workday._fetch_detail()` already appends to a tenant's CXS
    API base. Returns "" when the URL doesn't match the documented shape --
    never guessed."""
    path = urlparse(url or "").path or ""
    match = _SITE_PREFIX_RE.match(path)
    if not match:
        return ""
    return "/job/" + path[match.end():]


@dataclass(frozen=True)
class CanonicalIdentity:
    """The brief's "canonical job/application identity" for Workday."""
    recognized: bool
    tenant: str = ""
    site: str = ""
    host: str = ""
    requisition_id: str = ""
    external_path: str = ""
    canonical_url: str = ""
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "recognized": self.recognized, "tenant": self.tenant, "site": self.site, "host": self.host,
            "requisition_id": self.requisition_id, "external_path": self.external_path,
            "canonical_url": self.canonical_url, "reason": self.reason,
        }


def canonical_identity(job: Job) -> CanonicalIdentity:
    """Derived from the job's own real Workday URL via
    `app.applications.workday_tenant.parse_workday_tenant()` -- never
    fabricates a tenant/site/host it could not actually parse. `job.
    company_identifier` (the tenant alone, per `app.providers.workday`) is
    used only as a cross-check when both are available, never as the sole
    source (the site is not stored anywhere else on the job row)."""
    if (job.provider or "").lower() != "workday":
        return CanonicalIdentity(False, reason="job's provider is not workday")
    url = job.canonical_url or job.url or ""
    info: WorkdayTenantInfo = parse_workday_tenant(url)
    if not info.recognized or not info.tenant or not info.site:
        return CanonicalIdentity(
            False, reason="no tenant + site could be derived from the job's own URL",
        )
    external_path = _external_path(url)
    if not external_path:
        return CanonicalIdentity(
            False, tenant=info.tenant, site=info.site,
            reason="tenant + site were recognized, but no /job/... path suffix could be derived",
        )
    return CanonicalIdentity(
        True, tenant=info.tenant, site=info.site, host=info.host, requisition_id=info.requisition_id,
        external_path=external_path, canonical_url=url,
        reason="derived from the job's own real Workday URL via app.applications.workday_tenant.parse_workday_tenant",
    )


class WorkdayApplicationProvider(ApplicationProvider):
    name = "workday"
    capabilities = ApplicationCapabilities(
        provider="workday", provider_version="1.0.0",
        form_discovery_supported=False, field_mapping_supported=False,
        draft_fill_supported=False, file_upload_supported=False,
        submission_supported=False, confirmation_detection_supported=False,
        automation_policy=AutomationPolicy.ASSIST_ONLY, support_level=SupportLevel.UNSUPPORTED,
        live_validated=True,
        notes=(
            "Live-checked 2026-08-26 against a real public Workday tenant "
            "(walmart.wd504.myworkdayjobs.com/WalmartExternal, the same CXS API app.providers.workday "
            "already uses): the per-job detail endpoint's jobPostingInfo exposes canApply/jobReqId/"
            "questionnaireId/secondaryQuestionnaireId/... but questionnaireId is only an opaque reference "
            "-- no documented public endpoint returns its actual question content without an "
            "authenticated candidate session. API-side form discovery is therefore genuinely UNSUPPORTED, "
            "matching Lever's precedent, not guessed. The real form (and its typically account-gated apply "
            "flow) is reached, when at all, through the generic real-browser ASSIST engine instead -- see "
            "app.applications.browser_capability_matrix's workday row and app.applications.workday_tenant, "
            "which record genuinely VARIABLE per-tenant login behavior rather than a blanket claim. This "
            "adapter adds canonical (tenant, site, requisition id) identity derived from "
            "app.applications.workday_tenant.parse_workday_tenant(), and check_job_still_active()/"
            "classify_job_inactive_reason() from the SAME public detail endpoint's genuine canApply field "
            "(True/False) and HTTP 404/410. ASSIST_ONLY: submission is never attempted."
        ),
    )

    def __init__(self, client: Optional[httpx.Client] = None):
        self._client = client

    # --- identity -----------------------------------------------------------

    def detect_application(self, job: Job) -> bool:
        # Deliberately broader than `canonical_identity().recognized`, mirroring
        # LeverApplicationProvider: this is still the right handler for ANY
        # Workday job (it hands the candidate the real apply URL), even one
        # whose tenant/site could not be confidently parsed.
        return (job.provider or "").lower() == "workday"

    def canonical_identity(self, job: Job) -> CanonicalIdentity:
        return canonical_identity(job)

    def apply_url(self, job: Job) -> str:
        """The job's own real candidate-facing page IS the real apply
        entry point for Workday -- there is no separate published applyUrl
        field like Ashby's."""
        identity = canonical_identity(job)
        return identity.canonical_url or job.canonical_url or job.url or ""

    # --- shared public-API access --------------------------------------------

    def _fetch_detail(self, identity: CanonicalIdentity) -> tuple[Optional[dict], Optional[ProviderHTTPError]]:
        client = self._client or build_client(PROVIDER_HTTP_TIMEOUT_SECONDS)
        owns_client = self._client is None
        url = f"https://{identity.host}/wday/cxs/{identity.tenant}/{identity.site}{identity.external_path}"
        try:
            return get_json(client, url, provider="workday-application"), None
        except ProviderHTTPError as exc:
            return None, exc
        finally:
            if owns_client:
                client.close()

    # --- liveness -------------------------------------------------------------

    def check_job_still_active(self, job: Job) -> Optional[bool]:
        """Genuine evidence only: False on a permanent 404/410, or on the
        detail endpoint's own explicit `canApply: false`; True on a
        successful fetch with `canApply` True or absent; None ("not
        checkable") for a timeout, 5xx, refusal, or an unrecognized
        identity."""
        identity = canonical_identity(job)
        if not identity.recognized:
            return None
        payload, error = self._fetch_detail(identity)
        if error is not None:
            if error.status_code in _GONE_STATUS_CODES:
                return False
            return None
        info = (payload or {}).get("jobPostingInfo") or {}
        can_apply = info.get("canApply")
        if can_apply is False:
            return False
        return True

    def classify_job_inactive_reason(self, job: Job) -> Optional[str]:
        """HTTP 410 is an explicit "existed, now permanently gone" (EXPIRED);
        HTTP 404 is "no such posting" (REMOVED); an explicit `canApply:
        false` on an otherwise-reachable posting is the tenant's own genuine
        signal that it will not currently accept an application for this
        requisition (CLOSED). Anything else returns None so the caller falls
        back to a generic terminal blocker rather than inventing a reason."""
        identity = canonical_identity(job)
        if not identity.recognized:
            return None
        payload, error = self._fetch_detail(identity)
        if error is not None:
            if error.status_code == 410:
                return "EXPIRED"
            if error.status_code == 404:
                return "REMOVED"
            return None
        info = (payload or {}).get("jobPostingInfo") or {}
        if info.get("canApply") is False:
            return "CLOSED"
        return None

    # --- form flow (honestly unsupported on the API path) --------------------

    def discover_form(self, job: Job) -> Optional[FormSnapshot]:
        """Always None: no public Workday interface publishes the
        application field list without an authenticated candidate session.
        Never a hardcoded template. The browser-assist path discovers the
        real form from the rendered DOM instead, when reachable at all."""
        return None

    def map_fields(self, form: FormSnapshot, application_fields) -> MappingResult:
        return MappingResult()

    def fill_draft(self, form: FormSnapshot, mapping: MappingResult) -> DraftResult:
        return DraftResult(mapping=mapping, preserved=False)

    def validate(self, job: Job, form: FormSnapshot, draft: DraftResult) -> ValidationResult:
        return ValidationResult(
            ok=False, policy=AutomationPolicy.ASSIST_ONLY,
            policy_reasons=[PolicyReason.SUBMISSION_INTERFACE_UNSUPPORTED],
            detail=["Workday's form structure is not exposed by any public API without an authenticated "
                    "candidate session -- the real form, when reachable, is opened through browser assist "
                    "or manually via the apply URL."],
        )

    def submit(self, job: Job, form: FormSnapshot, draft: DraftResult) -> SubmitResult:
        return SubmitResult(success=False, error_type=PolicyReason.SUBMISSION_INTERFACE_UNSUPPORTED.value,
                             error_message_safe="workday: submission not supported.")

    def verify_confirmation(self, submit_result: SubmitResult) -> ConfirmationResult:
        return ConfirmationResult(confirmed=False)
