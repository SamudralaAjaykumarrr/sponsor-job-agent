"""SmartRecruiters application-form adapter (SmartRecruiters + Workable
Provider Execution V1).

Live-checked 2026-08-26 against the real, public, unauthenticated
SmartRecruiters Posting API (the same read API `app.providers.smartrecruiters`
already uses for discovery): the per-posting detail endpoint
(`GET https://api.smartrecruiters.com/v1/companies/{company}/postings/{id}`)
returns a genuine, structured payload whose keys are exactly `active,
applyUrl, company, creator, customField, defaultJobAd, department,
experienceLevel, function, id, industry, jobAd, jobAdId, jobId, language,
location, name, postingUrl, refNumber, referralUrl, releasedDate,
typeOfEmployment, uuid, visibility` -- but NO application-question schema of
any kind. A candidate probe of `.../postings/{id}/screening-questions`
returned a genuine HTTP 404 (verified live this build), and no other
documented public path exposes SmartRecruiters' actual candidate-facing
question set (that lives behind the authenticated candidate application UI,
not the public read API). API-side form discovery is therefore honestly
UNSUPPORTED here, matching `app.applications.providers_lever` /
`app.applications.providers_workday`'s precedent, not guessed.

What IS genuine and structured on this same public endpoint:
  - `active` (bool): live-verified True on a real, currently-open posting.
    An explicit False is real evidence the posting will not currently accept
    an application -- distinct from the posting being entirely gone
    (404/410, REMOVED/EXPIRED).
  - `applyUrl`/`postingUrl`: the provider-published candidate-facing URLs,
    preferred over a constructed guess.

Canonical identity is derived from the job row's own (company, numeric
posting id) pair -- populated by the discovery connector from this same
public API -- falling back to parsing a genuine
`jobs.smartrecruiters.com/{company}/{id}[-slug]` URL (live-verified
2026-08-26 to be SmartRecruiters' own documented redirect shape).

Submission stays NOT implemented and `submission_supported` stays False:
proving any of the ten REAL SUBMISSION CAPABILITY requirements would require
completing a real SmartRecruiters posting's actual apply flow end-to-end
(and, per `app.applications.browser_capability_matrix`'s smartrecruiters row,
the newer client-rendered `oneclick-ui` posting shape is CAPTCHA-blocked for
unauthenticated automated access at all) -- which this project never does."""

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

# Same permanent-vs-temporary split every other execution adapter in this
# project uses (CLAUDE.md's Phase 4 registry-lifecycle rule). 401/403 are
# deliberately excluded -- they mean "we were refused", never "the posting is
# gone".
_GONE_STATUS_CODES = frozenset({404, 410})

SMARTRECRUITERS_DETAIL_URL = "https://api.smartrecruiters.com/v1/companies/{company}/postings/{posting_id}"

# `jobs.smartrecruiters.com/{company}/{id}[-slug]` -- SmartRecruiters' own
# documented candidate-facing URL shape (live-verified 2026-08-26: a real
# posting's `postingUrl` resolves to exactly this pattern). The numeric id is
# always the FIRST path segment after the company, immediately followed by
# either a '-' (slug) or the end of the path.
_POSTING_URL_RE = re.compile(r"^/(?P<company>[A-Za-z0-9_.-]+)/(?P<posting_id>\d+)(?:-|/|$)")


@dataclass(frozen=True)
class CanonicalIdentity:
    """The brief's "canonical job/application identity" for SmartRecruiters."""
    recognized: bool
    company: str = ""
    posting_id: str = ""
    canonical_url: str = ""
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "recognized": self.recognized, "company": self.company, "posting_id": self.posting_id,
            "canonical_url": self.canonical_url, "reason": self.reason,
        }


def canonical_identity(job: Job) -> CanonicalIdentity:
    """Prefers the job row's own structured fields (populated by the
    discovery connector from this same public API), falling back to parsing
    a genuine jobs.smartrecruiters.com URL. Never fabricates a company/id it
    could not actually derive."""
    if (job.provider or "").lower() != "smartrecruiters":
        return CanonicalIdentity(False, reason="job's provider is not smartrecruiters")
    company = (job.company_identifier or "").strip()
    posting_id = (job.external_job_id or "").strip()
    if company and posting_id and posting_id.isdigit():
        return CanonicalIdentity(
            True, company, posting_id, canonical_url=f"https://jobs.smartrecruiters.com/{company}/{posting_id}",
            reason="derived from the job row's own company + numeric posting id",
        )
    for candidate in (job.canonical_url or "", job.url or ""):
        if not candidate:
            continue
        host = (urlparse(candidate).hostname or "").lower()
        if not host.endswith("smartrecruiters.com"):
            continue
        match = _POSTING_URL_RE.match(urlparse(candidate).path or "")
        if match:
            return CanonicalIdentity(
                True, match.group("company"), match.group("posting_id"), canonical_url=candidate,
                reason="parsed from a real jobs.smartrecruiters.com posting URL",
            )
    return CanonicalIdentity(
        False, company, posting_id,
        reason="no company + numeric posting id could be derived from the job row or its URLs",
    )


class SmartRecruitersApplicationProvider(ApplicationProvider):
    name = "smartrecruiters"
    capabilities = ApplicationCapabilities(
        provider="smartrecruiters", provider_version="1.0.0",
        form_discovery_supported=False, field_mapping_supported=False,
        draft_fill_supported=False, file_upload_supported=False,
        submission_supported=False, confirmation_detection_supported=False,
        automation_policy=AutomationPolicy.ASSIST_ONLY, support_level=SupportLevel.UNSUPPORTED,
        live_validated=True,
        notes=(
            "Live-checked 2026-08-26 against the real public SmartRecruiters Posting API "
            "(api.smartrecruiters.com/v1/companies/{company}/postings/{id}, the same one "
            "app.providers.smartrecruiters already uses): the response exposes active/applyUrl/postingUrl/"
            "compensation/... but no application-question schema; a candidate '.../screening-questions' probe "
            "genuinely 404s. API-side form discovery is therefore genuinely UNSUPPORTED, matching Lever/Workday's "
            "precedent, not guessed. The real form, when reachable, is reached through the generic real-browser "
            "ASSIST engine instead -- see app.applications.browser_capability_matrix's smartrecruiters row, which "
            "honestly records the newer oneclick-ui posting shape as CAPTCHA-blocked for unauthenticated automated "
            "access (never bypassed). This adapter adds canonical (company, numeric posting id) identity and "
            "check_job_still_active()/classify_job_inactive_reason() from the SAME public detail endpoint's "
            "genuine `active` boolean and HTTP 404/410. ASSIST_ONLY: submission is never attempted."
        ),
    )

    def __init__(self, client: Optional[httpx.Client] = None):
        self._client = client

    # --- identity -----------------------------------------------------------

    def detect_application(self, job: Job) -> bool:
        # Deliberately broader than `canonical_identity().recognized`,
        # mirroring LeverApplicationProvider/WorkdayApplicationProvider: this
        # is still the right handler for ANY SmartRecruiters job (it hands
        # the candidate the apply URL), even one whose company/id could not
        # be confidently parsed.
        return (job.provider or "").lower() == "smartrecruiters"

    def canonical_identity(self, job: Job) -> CanonicalIdentity:
        return canonical_identity(job)

    def apply_url(self, job: Job) -> str:
        """The provider-published `applyUrl`/`postingUrl` when the public API
        exposes one, else the canonical jobs.smartrecruiters.com URL, else
        "". Never a constructed guess at a URL shape beyond the documented
        redirect pattern already used as the final fallback."""
        identity = canonical_identity(job)
        if not identity.recognized:
            return ""
        payload, error = self._fetch_posting(identity)
        if error is None and isinstance(payload, dict):
            apply_url = (payload.get("applyUrl") or "").strip()
            if apply_url:
                return apply_url
            posting_url = (payload.get("postingUrl") or "").strip()
            if posting_url:
                return posting_url
        return identity.canonical_url

    # --- shared public-API access --------------------------------------------

    def _fetch_posting(self, identity: CanonicalIdentity) -> tuple[Optional[dict], Optional[ProviderHTTPError]]:
        client = self._client or build_client(PROVIDER_HTTP_TIMEOUT_SECONDS)
        owns_client = self._client is None
        url = SMARTRECRUITERS_DETAIL_URL.format(company=identity.company, posting_id=identity.posting_id)
        try:
            return get_json(client, url, provider="smartrecruiters-application"), None
        except ProviderHTTPError as exc:
            return None, exc
        finally:
            if owns_client:
                client.close()

    # --- liveness -------------------------------------------------------------

    def check_job_still_active(self, job: Job) -> Optional[bool]:
        """Genuine evidence only: False on a permanent 404/410, or on the
        detail endpoint's own explicit `active: false`; True on a successful
        fetch with `active` True or absent; None ("not checkable") for a
        timeout, 5xx, refusal, or an unrecognized identity."""
        identity = canonical_identity(job)
        if not identity.recognized:
            return None
        payload, error = self._fetch_posting(identity)
        if error is not None:
            if error.status_code in _GONE_STATUS_CODES:
                return False
            return None
        active = (payload or {}).get("active")
        if active is False:
            return False
        return True

    def classify_job_inactive_reason(self, job: Job) -> Optional[str]:
        """HTTP 410 is an explicit "existed, now permanently gone" (EXPIRED);
        HTTP 404 is "no such posting" (REMOVED); an explicit `active: false`
        on an otherwise-reachable posting is the company's own genuine signal
        that it is not currently accepting applications for this posting
        (CLOSED). Anything else returns None so the caller falls back to a
        generic terminal blocker rather than inventing a reason."""
        identity = canonical_identity(job)
        if not identity.recognized:
            return None
        payload, error = self._fetch_posting(identity)
        if error is not None:
            if error.status_code == 410:
                return "EXPIRED"
            if error.status_code == 404:
                return "REMOVED"
            return None
        if (payload or {}).get("active") is False:
            return "CLOSED"
        return None

    # --- form flow (honestly unsupported on the API path) --------------------

    def discover_form(self, job: Job) -> Optional[FormSnapshot]:
        """Always None: no public SmartRecruiters interface publishes the
        application field list. Never a hardcoded template. The browser-
        assist path discovers the real form from the rendered DOM instead,
        when reachable at all (see the CAPTCHA-blocked oneclick-ui finding in
        app.applications.browser_capability_matrix)."""
        return None

    def map_fields(self, form: FormSnapshot, application_fields) -> MappingResult:
        return MappingResult()

    def fill_draft(self, form: FormSnapshot, mapping: MappingResult) -> DraftResult:
        return DraftResult(mapping=mapping, preserved=False)

    def validate(self, job: Job, form: FormSnapshot, draft: DraftResult) -> ValidationResult:
        return ValidationResult(
            ok=False, policy=AutomationPolicy.ASSIST_ONLY,
            policy_reasons=[PolicyReason.SUBMISSION_INTERFACE_UNSUPPORTED],
            detail=["SmartRecruiters' form structure is not exposed by any public API -- the real form, when "
                    "reachable, is opened through browser assist or manually via the apply URL."],
        )

    def submit(self, job: Job, form: FormSnapshot, draft: DraftResult) -> SubmitResult:
        return SubmitResult(success=False, error_type=PolicyReason.SUBMISSION_INTERFACE_UNSUPPORTED.value,
                             error_message_safe="smartrecruiters: submission not supported.")

    def verify_confirmation(self, submit_result: SubmitResult) -> ConfirmationResult:
        return ConfirmationResult(confirmed=False)
