"""Workable application-form adapter (SmartRecruiters + Workable Provider
Execution V1).

Live-checked 2026-08-26 against the real, public, unauthenticated Workable
v2 job-detail API (the same one `app.providers.workable` already uses for
discovery): `GET https://apply.workable.com/api/v2/accounts/{account}/jobs/
{shortcode}` returns a genuine, structured payload whose top-level keys are
exactly `accountUid, approvalStatus, benefits, code, department, description,
id, isInternal, language, location, locations, published, remote,
requirements, shortcode, state, title, type, workplace` -- but NO
application-question/form schema of any kind. Two candidate probe paths
(`.../jobs/{shortcode}/application_form` on both the v3 and www.workable.com
widget hosts) were live-checked this build and both genuinely 404. API-side
form discovery is therefore honestly UNSUPPORTED here, matching
`app.applications.providers_lever`'s precedent, not guessed -- even though
`app.applications.browser_capability_matrix`'s workable row shows the real
rendered form IS reachable and LIVE_FORM_VERIFIED (14 real fields) through
the generic real-browser ASSIST engine, exactly the same "API gap the browser
engine closes" pattern already documented for Lever.

What IS genuine and structured on this same public endpoint:
  - `state` (str): live-verified "published" on a real, currently-listed
    job. Genuine evidence a posting is NOT published is therefore real
    evidence it will not currently accept an application -- distinct from
    the posting being entirely gone (a 404 on the detail endpoint itself,
    REMOVED). This project has not observed a real non-"published" value
    live, so this is used only as a binary "is it the one confirmed-open
    state" check, never as a claim about a specific closed-state string.

Canonical identity is derived from the job row's own (account, shortcode)
pair -- populated by the discovery connector from this same public API --
falling back to parsing a genuine `apply.workable.com/{account}/j/{shortcode}`
URL. Workable's real candidate-facing `url`/`application_url` (live-verified
2026-08-26 on a real posting) commonly omits the account segment entirely
(`apply.workable.com/j/{shortcode}`), so the job row's own stored account is
the PRIMARY identity source here, matching
`app.applications.providers_workday`'s precedent (URL alone is insufficient)
rather than Greenhouse/Lever's (URL alone is sufficient).

Submission stays NOT implemented and `submission_supported` stays False:
proving any of the ten REAL SUBMISSION CAPABILITY requirements would require
completing a real Workable tenant's actual apply flow end-to-end, which this
project never does."""

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
# project uses. 401/403 are deliberately excluded -- they mean "we were
# refused", never "the job is gone".
_GONE_STATUS_CODES = frozenset({404, 410})

WORKABLE_DETAIL_URL = "https://apply.workable.com/api/v2/accounts/{account}/jobs/{shortcode}"

# `apply.workable.com/{account}/j/{shortcode}[/...]` -- the account-qualified
# URL shape (app.providers.workable's own fallback URL construction). A real
# live posting's own `url` was observed to OMIT the account segment
# entirely (`apply.workable.com/j/{shortcode}`), which this regex correctly
# does not match -- see this module's docstring for why the job row's stored
# account is the primary identity source rather than the URL.
_ACCOUNT_URL_RE = re.compile(r"^/(?P<account>[A-Za-z0-9_-]+)/j/(?P<shortcode>[A-Za-z0-9]+)")


@dataclass(frozen=True)
class CanonicalIdentity:
    """The brief's "canonical job/application identity" for Workable."""
    recognized: bool
    account: str = ""
    shortcode: str = ""
    canonical_url: str = ""
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "recognized": self.recognized, "account": self.account, "shortcode": self.shortcode,
            "canonical_url": self.canonical_url, "reason": self.reason,
        }


def canonical_identity(job: Job) -> CanonicalIdentity:
    """Prefers the job row's own structured fields (populated by the
    discovery connector from this same public API), falling back to parsing
    an account-qualified workable.com URL. Never fabricates an account it
    could not actually derive -- a shortcode-only URL (Workable's own common
    real shape) is not enough on its own, since the v2 detail endpoint
    requires the account to look the job up at all."""
    if (job.provider or "").lower() != "workable":
        return CanonicalIdentity(False, reason="job's provider is not workable")
    account = (job.company_identifier or "").strip()
    shortcode = (job.external_job_id or "").strip()
    if account and shortcode:
        return CanonicalIdentity(
            True, account, shortcode,
            canonical_url=f"https://apply.workable.com/{account}/j/{shortcode}/",
            reason="derived from the job row's own account + shortcode",
        )
    for candidate in (job.canonical_url or "", job.url or ""):
        if not candidate:
            continue
        host = (urlparse(candidate).hostname or "").lower()
        if not host.endswith("workable.com"):
            continue
        match = _ACCOUNT_URL_RE.match(urlparse(candidate).path or "")
        if match:
            return CanonicalIdentity(
                True, match.group("account"), match.group("shortcode"), canonical_url=candidate,
                reason="parsed from a real account-qualified workable.com posting URL",
            )
    return CanonicalIdentity(
        False, account, shortcode,
        reason="no account + shortcode could be derived from the job row or its URLs "
               "(a shortcode-only URL cannot be resolved without the account)",
    )


class WorkableApplicationProvider(ApplicationProvider):
    name = "workable"
    capabilities = ApplicationCapabilities(
        provider="workable", provider_version="1.0.0",
        form_discovery_supported=False, field_mapping_supported=False,
        draft_fill_supported=False, file_upload_supported=False,
        submission_supported=False, confirmation_detection_supported=False,
        automation_policy=AutomationPolicy.ASSIST_ONLY, support_level=SupportLevel.UNSUPPORTED,
        live_validated=True,
        notes=(
            "Live-checked 2026-08-26 against the real public Workable v2 job-detail API "
            "(apply.workable.com/api/v2/accounts/{account}/jobs/{shortcode}, the same one "
            "app.providers.workable already uses): the response exposes state/description/requirements/"
            "benefits/workplace/... but no application-question/form schema; two candidate "
            "'.../application_form' probe paths genuinely 404. API-side form discovery is therefore genuinely "
            "UNSUPPORTED, matching Lever's precedent, not guessed -- even though the real rendered form IS "
            "LIVE_FORM_VERIFIED (14 real fields) through the generic real-browser ASSIST engine (see "
            "app.applications.browser_capability_matrix's workable row). This adapter adds canonical "
            "(account, shortcode) identity and check_job_still_active()/classify_job_inactive_reason() from the "
            "SAME public detail endpoint's genuine `state` field and HTTP 404. ASSIST_ONLY: submission is never "
            "attempted."
        ),
    )

    def __init__(self, client: Optional[httpx.Client] = None):
        self._client = client

    # --- identity -----------------------------------------------------------

    def detect_application(self, job: Job) -> bool:
        # Deliberately broader than `canonical_identity().recognized`,
        # mirroring LeverApplicationProvider/WorkdayApplicationProvider: this
        # is still the right handler for ANY Workable job (it hands the
        # candidate the apply URL), even one whose account/shortcode could
        # not be confidently derived.
        return (job.provider or "").lower() == "workable"

    def canonical_identity(self, job: Job) -> CanonicalIdentity:
        return canonical_identity(job)

    def apply_url(self, job: Job) -> str:
        """The job's own stored candidate-facing URL when available (it is
        already the real apply form directly -- see
        app.providers.workable's own live-verified `application_url`
        preference), else the constructed canonical URL. Never a guess."""
        return job.canonical_url or job.url or canonical_identity(job).canonical_url

    # --- shared public-API access --------------------------------------------

    def _fetch_detail(self, identity: CanonicalIdentity) -> tuple[Optional[dict], Optional[ProviderHTTPError]]:
        client = self._client or build_client(PROVIDER_HTTP_TIMEOUT_SECONDS)
        owns_client = self._client is None
        url = WORKABLE_DETAIL_URL.format(account=identity.account, shortcode=identity.shortcode)
        try:
            return get_json(client, url, provider="workable-application"), None
        except ProviderHTTPError as exc:
            return None, exc
        finally:
            if owns_client:
                client.close()

    # --- liveness -------------------------------------------------------------

    def check_job_still_active(self, job: Job) -> Optional[bool]:
        """Genuine evidence only: False on a permanent 404/410, or on the
        detail endpoint's own `state` field genuinely reading something other
        than "published"; True on a successful fetch with `state ==
        "published"` or absent; None ("not checkable") for a timeout, 5xx,
        refusal, or an unrecognized identity."""
        identity = canonical_identity(job)
        if not identity.recognized:
            return None
        payload, error = self._fetch_detail(identity)
        if error is not None:
            if error.status_code in _GONE_STATUS_CODES:
                return False
            return None
        state = (payload or {}).get("state")
        if state is not None and str(state).strip().lower() != "published":
            return False
        return True

    def classify_job_inactive_reason(self, job: Job) -> Optional[str]:
        """HTTP 410 is an explicit "existed, now permanently gone" (EXPIRED);
        HTTP 404 is "no such job" (REMOVED); a `state` genuinely present but
        not "published" on an otherwise-reachable job is the account's own
        genuine signal that it is not currently open (CLOSED). Anything else
        returns None so the caller falls back to a generic terminal blocker
        rather than inventing a reason."""
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
        state = (payload or {}).get("state")
        if state is not None and str(state).strip().lower() != "published":
            return "CLOSED"
        return None

    # --- form flow (honestly unsupported on the API path) --------------------

    def discover_form(self, job: Job) -> Optional[FormSnapshot]:
        """Always None: no public Workable interface publishes the
        application field list. Never a hardcoded template. The browser-
        assist path discovers the real, LIVE_FORM_VERIFIED form from the
        rendered DOM instead."""
        return None

    def map_fields(self, form: FormSnapshot, application_fields) -> MappingResult:
        return MappingResult()

    def fill_draft(self, form: FormSnapshot, mapping: MappingResult) -> DraftResult:
        return DraftResult(mapping=mapping, preserved=False)

    def validate(self, job: Job, form: FormSnapshot, draft: DraftResult) -> ValidationResult:
        return ValidationResult(
            ok=False, policy=AutomationPolicy.ASSIST_ONLY,
            policy_reasons=[PolicyReason.SUBMISSION_INTERFACE_UNSUPPORTED],
            detail=["Workable's form structure is not exposed by any public API -- the real form is reached "
                    "through browser assist, or opened manually via the apply URL."],
        )

    def submit(self, job: Job, form: FormSnapshot, draft: DraftResult) -> SubmitResult:
        return SubmitResult(success=False, error_type=PolicyReason.SUBMISSION_INTERFACE_UNSUPPORTED.value,
                             error_message_safe="workable: submission not supported.")

    def verify_confirmation(self, submit_result: SubmitResult) -> ConfirmationResult:
        return ConfirmationResult(confirmed=False)
