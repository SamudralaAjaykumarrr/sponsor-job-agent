"""Lever application-form adapter (CLAUDE.md Phase 8 section 25;
strengthened by Real Provider Execution V1).

Live-checked during Phase 8's development, and unchanged since: Lever's
public postings API (`api.lever.co/v0/postings/{site}?mode=json`, and the
per-posting `api.lever.co/v0/postings/{site}/{id}`) exposes only
`hostedUrl`/`applyUrl` for a posting -- **no structured custom-question
schema is present anywhere in the response**. Unlike Greenhouse, there is no
documented public endpoint that returns Lever's actual application field
list, so API-side form discovery is honestly UNSUPPORTED here rather than
guessed from a hardcoded "typical Lever form" template (which would risk
silently going stale or simply being wrong -- CLAUDE.md's "never inflate a
capability, never fabricate a field a provider doesn't expose" rule).

That is a limitation of the API, NOT of this project's ability to reach a
Lever form. The generic real-browser engine
(`app.applications.browser_runtime`) reads the rendered DOM directly and has
LIVE-VERIFIED 22 real fields on Lever's own public demo posting -- see
`app.applications.browser_capability_matrix`'s lever row. The unified
`app.applications.execution_contract` reports that honestly:
`form_discovery_supported=True` with
`form_discovery_source=BROWSER_LIVE_VERIFIED`, while THIS adapter's
`ApplicationCapabilities.form_discovery_supported` correctly stays False,
because the two describe different interfaces.

Real Provider Execution V1 additions, all built strictly on the SAME public
read API already used by the discovery connector:

  - Canonical posting identity (`canonical_identity()`): the (site, posting
    id) pair plus the canonical hosted URL, from the job row or parsed from
    a real lever.co URL. Lever posting ids are UUIDs -- never numeric -- so
    a confidently-shaped id is genuinely verifiable.
  - `check_job_still_active()` / `classify_job_inactive_reason()`: a
    permanent 404/410 on the per-posting endpoint is real evidence the
    posting is gone; a timeout/5xx/403 returns None ("not checkable"),
    never False.
  - `apply_url()`: the provider-published `applyUrl` (the real form),
    preferred over the more generic hosted job-description URL.

Submission stays NOT implemented and `submission_supported` stays False:
Lever's apply endpoint is not a documented public programmatic interface,
and none of the brief's ten REAL SUBMISSION CAPABILITY requirements could be
proven end-to-end without submitting a real application to a real employer,
which this project never does.
"""

import logging
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
from app.config import PROVIDER_HTTP_TIMEOUT_SECONDS
from app.models import Job
from app.providers.http_client import ProviderHTTPError, build_client, get_json

logger = logging.getLogger("applications.lever")

LEVER_POSTING_URL = "https://api.lever.co/v0/postings/{site}/{posting_id}"

# Same permanent-vs-temporary split the Greenhouse adapter and CLAUDE.md's
# Phase 4 registry lifecycle rules use. 401/403 are deliberately excluded.
_GONE_STATUS_CODES = frozenset({404, 410})

# jobs.lever.co/<site>/<uuid>[/apply] -- Lever identifies a posting by a UUID
# path segment (verified live; see app.applications.job_identity's own
# `_PATH_UUID_RE`, which relies on the same fact).
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE,
)
_HOSTED_URL_RE = re.compile(
    r"/(?P<site>[A-Za-z0-9_.-]+)/(?P<posting_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CanonicalIdentity:
    """The brief's "canonical posting identity" for Lever."""
    recognized: bool
    site: str = ""
    posting_id: str = ""
    canonical_url: str = ""
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "recognized": self.recognized, "site": self.site, "posting_id": self.posting_id,
            "canonical_url": self.canonical_url, "reason": self.reason,
        }


def canonical_identity(job: Job) -> CanonicalIdentity:
    """Prefers the job row's own structured fields (populated by the
    discovery connector from this same public API), falling back to parsing
    a genuine lever.co URL. A posting id is only accepted when it is a real
    UUID -- Lever's actual id shape -- so a placeholder/numeric value can
    never masquerade as one."""
    if (job.provider or "").lower() != "lever":
        return CanonicalIdentity(False, reason="job's provider is not lever")
    site = (job.company_identifier or "").strip()
    posting_id = (job.external_job_id or "").strip()
    if site and posting_id and _UUID_RE.match(posting_id):
        return CanonicalIdentity(
            True, site, posting_id, canonical_url=f"https://jobs.lever.co/{site}/{posting_id}",
            reason="derived from the job row's own site + posting id",
        )
    for candidate in (job.canonical_url or "", job.url or ""):
        if not candidate:
            continue
        host = (urlparse(candidate).hostname or "").lower()
        if not (host.endswith("lever.co") or host.endswith("lever.com")):
            continue
        match = _HOSTED_URL_RE.search(urlparse(candidate).path or "")
        if match:
            return CanonicalIdentity(
                True, match.group("site"), match.group("posting_id").lower(), canonical_url=candidate,
                reason="parsed from a real lever.co posting URL",
            )
    return CanonicalIdentity(
        False, site, posting_id,
        reason="no site + UUID posting id could be derived from the job row or its URLs",
    )


class LeverApplicationProvider(ApplicationProvider):
    name = "lever"
    capabilities = ApplicationCapabilities(
        provider="lever", provider_version="1.1.0",
        form_discovery_supported=False, field_mapping_supported=False,
        draft_fill_supported=False, file_upload_supported=False,
        submission_supported=False, confirmation_detection_supported=False,
        automation_policy=AutomationPolicy.ASSIST_ONLY, support_level=SupportLevel.UNSUPPORTED,
        live_validated=True,
        notes=(
            "Live-checked: Lever's public postings API exposes only hostedUrl/applyUrl, no structured "
            "question schema -- API-side form discovery is genuinely UNSUPPORTED, not guessed. The real "
            "form IS reachable, and was live-verified with 22 real fields, through the generic real-browser "
            "engine instead (see app.applications.browser_capability_matrix's lever row and "
            "app.applications.execution_contract, which reports form_discovery_source=BROWSER_LIVE_VERIFIED). "
            "Real Provider Execution V1 added canonical (site, UUID posting id) identity, applyUrl "
            "resolution, and the optional check_job_still_active()/classify_job_inactive_reason() hooks "
            "on the same public read API. ASSIST_ONLY: submission is never attempted."
        ),
    )

    def __init__(self, client: Optional[httpx.Client] = None):
        self._client = client

    # --- identity ---------------------------------------------------------

    def detect_application(self, job: Job) -> bool:
        # Deliberately broader than `canonical_identity().recognized`: this
        # adapter is still the right handler for ANY Lever job (it hands the
        # candidate the apply URL), even one whose posting id we could not
        # confidently shape-check. The identity check is what gates the
        # API-backed liveness lookups below, not provider selection.
        return (job.provider or "").lower() == "lever"

    def canonical_identity(self, job: Job) -> CanonicalIdentity:
        return canonical_identity(job)

    def apply_url(self, job: Job) -> str:
        """The provider-published `applyUrl` when the public API exposes one
        (it is the real form directly, avoiding an unnecessary landing-page
        hop), else the canonical hosted URL, else "". Never a constructed
        guess at a URL shape."""
        identity = canonical_identity(job)
        if not identity.recognized:
            return ""
        payload, error = self._fetch_posting(identity)
        if error is None and isinstance(payload, dict):
            apply_url = (payload.get("applyUrl") or "").strip()
            if apply_url:
                return apply_url
            hosted = (payload.get("hostedUrl") or "").strip()
            if hosted:
                return hosted
        return identity.canonical_url

    # --- shared public-API access ----------------------------------------

    def _fetch_posting(self, identity: CanonicalIdentity) -> tuple[Optional[dict], Optional[ProviderHTTPError]]:
        client = self._client or build_client(PROVIDER_HTTP_TIMEOUT_SECONDS)
        owns_client = self._client is None
        url = LEVER_POSTING_URL.format(site=identity.site, posting_id=identity.posting_id)
        try:
            return get_json(client, url, provider="lever-application"), None
        except ProviderHTTPError as exc:
            return None, exc
        finally:
            if owns_client:
                client.close()

    # --- liveness ---------------------------------------------------------

    def check_job_still_active(self, job: Job) -> Optional[bool]:
        """Genuine evidence only: True on a successful public-API lookup,
        False on a permanent 404/410, None ("not checkable") for a timeout,
        5xx, refusal, or an unrecognized identity."""
        identity = canonical_identity(job)
        if not identity.recognized:
            return None
        _payload, error = self._fetch_posting(identity)
        if error is None:
            return True
        if error.status_code in _GONE_STATUS_CODES:
            return False
        return None

    def classify_job_inactive_reason(self, job: Job) -> Optional[str]:
        """HTTP 410 Gone is an explicit "existed, now permanently gone"
        (EXPIRED); 404 is "no such posting" (REMOVED). Anything else returns
        None so the caller falls back to a generic terminal blocker rather
        than inventing a reason."""
        identity = canonical_identity(job)
        if not identity.recognized:
            return None
        _payload, error = self._fetch_posting(identity)
        if error is None:
            return None
        if error.status_code == 410:
            return "EXPIRED"
        if error.status_code == 404:
            return "REMOVED"
        return None

    # --- form flow (honestly unsupported on the API path) -----------------

    def discover_form(self, job: Job) -> Optional[FormSnapshot]:
        """Always None: no public Lever interface publishes the application
        field list. Never a hardcoded template. The browser-assist path
        discovers the real form from the rendered DOM instead."""
        return None

    def map_fields(self, form: FormSnapshot, application_fields) -> MappingResult:
        return MappingResult()

    def fill_draft(self, form: FormSnapshot, mapping: MappingResult) -> DraftResult:
        return DraftResult(mapping=mapping, preserved=False)

    def validate(self, job: Job, form: FormSnapshot, draft: DraftResult) -> ValidationResult:
        return ValidationResult(
            ok=False, policy=AutomationPolicy.ASSIST_ONLY,
            policy_reasons=[PolicyReason.SUBMISSION_INTERFACE_UNSUPPORTED],
            detail=["Lever's form structure is not exposed by any public API -- the real form is reached "
                    "through browser assist, or opened manually via the apply URL."],
        )

    def submit(self, job: Job, form: FormSnapshot, draft: DraftResult) -> SubmitResult:
        return SubmitResult(success=False, error_type=PolicyReason.SUBMISSION_INTERFACE_UNSUPPORTED.value,
                             error_message_safe="lever: submission not supported.")

    def verify_confirmation(self, submit_result: SubmitResult) -> ConfirmationResult:
        return ConfirmationResult(confirmed=False)
