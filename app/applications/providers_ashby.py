"""Ashby application-form adapter (Workday + Ashby Provider Execution V1).

Live-checked 2026-08-26 against Ashby's real public job-board API
(`api.ashbyhq.com/posting-api/job-board/{boardName}`, the same endpoint
`app.providers.ashby` already uses for discovery): a job entry's ONLY fields
are `id, title, department, team, employmentType, location,
secondaryLocations, compensation, descriptionHtml, descriptionPlain,
applyUrl, jobUrl, publishedAt, isRemote, isListed, workplaceType, address` --
no structured custom-question schema of any kind. `applyUrl` is the API's own
published `/application`-suffixed form URL (distinct from the plain `jobUrl`
job-description page), and job ids are genuine UUIDs (e.g.
"7458d4e9-da2e-47bd-98cb-adfda43d42b2"). API-side form discovery is
therefore honestly UNSUPPORTED, matching
`app.applications.providers_lever`'s precedent, not guessed. The real form IS
reachable, and was live-verified with 27-28 real fields, through the generic
real-browser engine instead (see `app.applications.browser_capability_matrix`'s
ashby row).

There is no documented public per-id GET endpoint -- only the full-board list
this project's discovery connector already uses. `check_job_still_active()`
therefore re-fetches that same board and checks genuine membership by id: a
job absent from a fresh fetch is real (if coarse) evidence the board no
longer lists it. This can only ever report "REMOVED" (never a more specific
EXPIRED/CLOSED), since list absence alone cannot distinguish those -- an
honest limitation of the only public interface available, not an invented
distinction.

Submission stays NOT implemented and `submission_supported` stays False:
proving any of the ten REAL SUBMISSION CAPABILITY requirements would require
completing a real employer's actual Ashby apply flow end-to-end, which this
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
from app.providers.ashby import ASHBY_JOB_BOARD_URL
from app.providers.http_client import ProviderHTTPError, build_client, get_json

# Ashby job/posting ids are genuine UUIDs (live-verified) -- never numeric or
# a placeholder string, so a confidently-shaped id is genuinely verifiable,
# mirroring app.applications.providers_lever's own `_UUID_RE`.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE,
)
# jobs.ashbyhq.com/<board>/<uuid>[/application] -- the board name + job id
# path segments, live-verified against a real applyUrl/jobUrl pair.
_JOB_URL_RE = re.compile(
    r"/(?P<board>[A-Za-z0-9_.-]+)/(?P<job_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CanonicalIdentity:
    """The brief's "canonical job/application identity" for Ashby."""
    recognized: bool
    board_name: str = ""
    job_id: str = ""
    canonical_url: str = ""
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "recognized": self.recognized, "board_name": self.board_name, "job_id": self.job_id,
            "canonical_url": self.canonical_url, "reason": self.reason,
        }


def canonical_identity(job: Job) -> CanonicalIdentity:
    """Prefers the job row's own structured fields (populated by the
    discovery connector from this same public API), falling back to parsing
    a genuine ashbyhq.com URL. A job id is only accepted when it is a real
    UUID -- Ashby's actual id shape -- so a placeholder value can never
    masquerade as one."""
    if (job.provider or "").lower() != "ashby":
        return CanonicalIdentity(False, reason="job's provider is not ashby")
    board = (job.company_identifier or "").strip()
    job_id = (job.external_job_id or "").strip()
    if board and job_id and _UUID_RE.match(job_id):
        return CanonicalIdentity(
            True, board, job_id.lower(), canonical_url=f"https://jobs.ashbyhq.com/{board}/{job_id}",
            reason="derived from the job row's own board name + posting id",
        )
    for candidate in (job.canonical_url or "", job.url or ""):
        if not candidate:
            continue
        host = (urlparse(candidate).hostname or "").lower()
        if not host.endswith("ashbyhq.com"):
            continue
        match = _JOB_URL_RE.search(urlparse(candidate).path or "")
        if match:
            return CanonicalIdentity(
                True, match.group("board"), match.group("job_id").lower(), canonical_url=candidate,
                reason="parsed from a real ashbyhq.com posting URL",
            )
    return CanonicalIdentity(
        False, board, job_id,
        reason="no board name + UUID posting id could be derived from the job row or its URLs",
    )


class AshbyApplicationProvider(ApplicationProvider):
    name = "ashby"
    capabilities = ApplicationCapabilities(
        provider="ashby", provider_version="1.0.0",
        form_discovery_supported=False, field_mapping_supported=False,
        draft_fill_supported=False, file_upload_supported=False,
        submission_supported=False, confirmation_detection_supported=False,
        automation_policy=AutomationPolicy.ASSIST_ONLY, support_level=SupportLevel.UNSUPPORTED,
        live_validated=True,
        notes=(
            "Live-checked 2026-08-26 against Ashby's public job-board API (api.ashbyhq.com/posting-api/"
            "job-board/{board}, the same endpoint app.providers.ashby already uses): a job entry's only "
            "fields are id/title/department/team/employmentType/location/secondaryLocations/compensation/"
            "descriptionHtml/descriptionPlain/applyUrl/jobUrl/publishedAt/isRemote/isListed/workplaceType/"
            "address -- no structured custom-question schema of any kind. API-side form discovery is "
            "therefore genuinely UNSUPPORTED, matching Lever's precedent, not guessed. The real form IS "
            "reachable, and was live-verified with 27-28 real fields, through the generic real-browser "
            "engine instead (see app.applications.browser_capability_matrix's ashby row). This adapter "
            "adds canonical (board name, UUID job id) identity, applyUrl resolution (the API's own "
            "published '/application'-suffixed URL), and check_job_still_active()/"
            "classify_job_inactive_reason() via genuine board-list membership (the API exposes no per-id "
            "GET endpoint, so absence from a fresh full-board fetch is the honest evidence available -- "
            "reported as REMOVED, since CLOSED vs EXPIRED cannot be distinguished from list absence "
            "alone). ASSIST_ONLY: submission is never attempted."
        ),
    )

    def __init__(self, client: Optional[httpx.Client] = None):
        self._client = client

    # --- identity -------------------------------------------------------------

    def detect_application(self, job: Job) -> bool:
        # Deliberately broader than `canonical_identity().recognized`,
        # mirroring LeverApplicationProvider: still the right handler for ANY
        # Ashby job, even one whose id could not be confidently shape-checked.
        return (job.provider or "").lower() == "ashby"

    def canonical_identity(self, job: Job) -> CanonicalIdentity:
        return canonical_identity(job)

    # --- shared public-API access ----------------------------------------------

    def _fetch_board(self, board_name: str) -> tuple[Optional[dict], Optional[ProviderHTTPError]]:
        client = self._client or build_client(PROVIDER_HTTP_TIMEOUT_SECONDS)
        owns_client = self._client is None
        url = ASHBY_JOB_BOARD_URL.format(board_name=board_name)
        try:
            return get_json(client, url, provider="ashby-application", params={"includeCompensation": "true"}), None
        except ProviderHTTPError as exc:
            return None, exc
        finally:
            if owns_client:
                client.close()

    def _find_entry(self, identity: CanonicalIdentity) -> tuple[Optional[dict], Optional[ProviderHTTPError]]:
        payload, error = self._fetch_board(identity.board_name)
        if error is not None:
            return None, error
        for entry in (payload or {}).get("jobs") or []:
            if str(entry.get("id", "")).lower() == identity.job_id:
                return entry, None
        return None, None

    def apply_url(self, job: Job) -> str:
        """The API's own published `applyUrl` when it's genuinely present
        (the real form directly, avoiding an unnecessary landing-page hop),
        else `jobUrl`, else the canonical URL. Never a constructed guess."""
        identity = canonical_identity(job)
        if identity.recognized:
            entry, error = self._find_entry(identity)
            if error is None and entry is not None:
                apply_url = (entry.get("applyUrl") or "").strip()
                if apply_url:
                    return apply_url
                job_url = (entry.get("jobUrl") or "").strip()
                if job_url:
                    return job_url
        return identity.canonical_url or job.canonical_url or job.url or ""

    # --- liveness ---------------------------------------------------------------

    def check_job_still_active(self, job: Job) -> Optional[bool]:
        """Genuine evidence only: True when the job id is still present in a
        fresh fetch of its board, False when the board was fetched
        successfully but the id is genuinely absent, None ("not checkable")
        on a fetch failure/refusal or an unrecognized identity -- a
        board-level failure must never be mistaken for "this one job is
        gone"."""
        identity = canonical_identity(job)
        if not identity.recognized:
            return None
        payload, error = self._fetch_board(identity.board_name)
        if error is not None:
            return None
        ids = {str(entry.get("id", "")).lower() for entry in (payload or {}).get("jobs") or []}
        return identity.job_id in ids

    def classify_job_inactive_reason(self, job: Job) -> Optional[str]:
        """Only ever "REMOVED" -- list-absence is the only evidence this
        public API can offer, and it cannot distinguish EXPIRED from CLOSED.
        Anything else (a fetch failure, or the job still being listed)
        returns None so the caller falls back to a generic terminal blocker
        rather than inventing a finer-grained reason than the evidence
        supports."""
        identity = canonical_identity(job)
        if not identity.recognized:
            return None
        payload, error = self._fetch_board(identity.board_name)
        if error is not None:
            return None
        ids = {str(entry.get("id", "")).lower() for entry in (payload or {}).get("jobs") or []}
        if identity.job_id not in ids:
            return "REMOVED"
        return None

    # --- form flow (honestly unsupported on the API path) ------------------------

    def discover_form(self, job: Job) -> Optional[FormSnapshot]:
        """Always None: no public Ashby interface publishes the application
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
            detail=["Ashby's form structure is not exposed by any public API -- the real form is reached "
                    "through browser assist, or opened manually via the apply URL."],
        )

    def submit(self, job: Job, form: FormSnapshot, draft: DraftResult) -> SubmitResult:
        return SubmitResult(success=False, error_type=PolicyReason.SUBMISSION_INTERFACE_UNSUPPORTED.value,
                             error_message_safe="ashby: submission not supported.")

    def verify_confirmation(self, submit_result: SubmitResult) -> ConfirmationResult:
        return ConfirmationResult(confirmed=False)
