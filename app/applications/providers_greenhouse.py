"""Greenhouse application-form adapter (CLAUDE.md Phase 8 section 24;
strengthened by Real Provider Execution V1).

Form discovery is LIVE-VALIDATED against the real, public, unauthenticated
Greenhouse Job Board API endpoint
`https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}?questions=true`
(https://developers.greenhouse.io/job-board.html) -- confirmed during Phase
8's own development to return genuine structured application-question fields
(name/label/type/required/choices), including the EEOC demographic questions
and, on the specific posting checked, a real sponsorship question. This is
the same officially documented read API the discovery connector
(app.providers.greenhouse) already uses, just with the `questions=true` flag.

Submission is explicitly NOT implemented, and `submission_supported` stays
False. The actual "apply" action on a Greenhouse job board goes through the
site's own embedded, CSRF-protected form flow -- not the documented public
Job Board API -- so automating it would mean reverse-engineering an
undocumented interface rather than using one explicitly permitted for
programmatic use. Every one of the brief's ten REAL SUBMISSION CAPABILITY
requirements would additionally have to be proven end-to-end against a real
employer's posting, which this project never does. Per CLAUDE.md's own
instruction the adapter therefore stays ASSIST_ONLY.

Real Provider Execution V1 additions, all built strictly on the SAME public
read API (no new interface, no scraping of the apply flow):

  - Canonical application identity (`canonical_identity()`): the
    (board token, posting id) pair plus the canonical board URL, derived
    from the job row or, failing that, parsed from a real greenhouse.io URL.
  - A typed discovery OUTCOME (`discover_form_detailed()`) so an expired/
    removed posting (a permanent 404/410 from the public API) is reported
    distinctly from a transient network failure, instead of both collapsing
    into `discover_form() -> None`. `discover_form()` itself keeps its exact
    previous signature and behavior -- the ApplicationProvider contract is
    unchanged.
  - `check_job_still_active()` / `classify_job_inactive_reason()`, the two
    OPTIONAL hooks the executor calls immediately before any submission
    step. They only ever return genuine evidence obtained from the public
    API; a temporary failure returns None ("not checkable"), never False.
  - `normalized_form()`, projecting the discovered schema into the shared
    provider-neutral `app.applications.form_model` shape.

What this adapter deliberately does NOT claim: it cannot detect a CAPTCHA,
an auth wall, or a rendered confirmation page, because the public JSON read
API never shows any of those -- those are genuinely the browser-assist
layer's observations (`app.applications.browser_runtime`), and inventing an
API-side signal for them would be exactly the kind of inflated capability
CLAUDE.md forbids.
"""

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from urllib.parse import urlparse

import httpx

from app.applications.form_model import FormFieldSource, NormalizedForm, normalize_form_snapshot
from app.applications.mapping import match_field
from app.applications.models import (
    ApplicationCapabilities,
    ApplicationField,
    AutomationPolicy,
    DraftResult,
    FieldConfidence,
    FormField,
    FormSnapshot,
    MappedField,
    MappingResult,
    PolicyReason,
    SupportLevel,
    ValidationResult,
)
from app.applications.provider import ApplicationProvider
from app.applications.schema import DECLINE_TO_SELF_IDENTIFY_PHRASES, find_field
from app.config import PROVIDER_HTTP_TIMEOUT_SECONDS
from app.models import Job
from app.providers.http_client import ProviderHTTPError, build_client, get_json

logger = logging.getLogger("applications.greenhouse")

GREENHOUSE_JOB_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}"

# Status codes that are PERMANENT evidence the posting itself is gone, as
# opposed to a temporary problem. Mirrors CLAUDE.md's Phase 4 registry
# permanent-vs-temporary split; 401/403 are deliberately NOT here -- they
# mean "we were refused", never "the job expired".
_GONE_STATUS_CODES = frozenset({404, 410})

# `boards.greenhouse.io/<token>/jobs/<id>` and the newer
# `job-boards.greenhouse.io/<token>/jobs/<id>` shape (a real, organic host
# migration this project observed live between Phase 11 and Phase 12).
_BOARD_URL_RE = re.compile(r"/(?P<token>[A-Za-z0-9_.-]+)/jobs/(?P<job_id>\d+)")

_TYPE_MAP = {
    "input_text": "input_text",
    "input_file": "input_file",
    "textarea": "textarea",
    "multi_value_single_select": "multi_value_single_select",
}


class FormDiscoveryOutcome(str, Enum):
    """Real Provider Execution V1: why a form-discovery attempt ended the way
    it did. `discover_form()` collapses all the non-DISCOVERED values to
    None (its unchanged contract); `discover_form_detailed()` preserves the
    distinction so the executor/dashboard can raise an honest terminal
    "job expired" blocker instead of a generic failure."""
    DISCOVERED = "DISCOVERED"
    NOT_APPLICABLE = "NOT_APPLICABLE"          # this job isn't a Greenhouse posting we can identify
    JOB_GONE = "JOB_GONE"                      # permanent 404/410 from the public API
    ACCESS_REFUSED = "ACCESS_REFUSED"          # 401/403 -- refused, never worked around
    TEMPORARY_FAILURE = "TEMPORARY_FAILURE"    # network/5xx/timeout -- retryable, never terminal
    NO_QUESTIONS_EXPOSED = "NO_QUESTIONS_EXPOSED"  # 200 OK, but the board publishes no question schema


@dataclass(frozen=True)
class CanonicalIdentity:
    """The brief's "canonical job/application identity" for Greenhouse."""
    recognized: bool
    board_token: str = ""
    posting_id: str = ""
    canonical_url: str = ""
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "recognized": self.recognized, "board_token": self.board_token, "posting_id": self.posting_id,
            "canonical_url": self.canonical_url, "reason": self.reason,
        }


@dataclass
class FormDiscoveryResult:
    outcome: FormDiscoveryOutcome
    form: Optional[FormSnapshot] = None
    identity: CanonicalIdentity = CanonicalIdentity(False)
    status_code: Optional[int] = None
    detail: str = ""


def canonical_identity(job: Job) -> CanonicalIdentity:
    """Prefers the job row's own structured fields (which the discovery
    connector populated from the same public API), falling back to parsing a
    genuine greenhouse.io URL. Never fabricates a token/id it could not
    actually derive."""
    if (job.provider or "").lower() != "greenhouse":
        return CanonicalIdentity(False, reason="job's provider is not greenhouse")
    token = (job.company_identifier or "").strip()
    posting_id = (job.external_job_id or "").strip()
    if token and posting_id:
        return CanonicalIdentity(
            True, token, posting_id,
            canonical_url=f"https://boards.greenhouse.io/{token}/jobs/{posting_id}",
            reason="derived from the job row's own board token + posting id",
        )
    for candidate in (job.canonical_url or "", job.url or ""):
        if not candidate:
            continue
        host = (urlparse(candidate).hostname or "").lower()
        if not host.endswith("greenhouse.io"):
            continue
        match = _BOARD_URL_RE.search(urlparse(candidate).path or "")
        if match:
            return CanonicalIdentity(
                True, match.group("token"), match.group("job_id"), canonical_url=candidate,
                reason="parsed from a real greenhouse.io board URL",
            )
    return CanonicalIdentity(
        False, token, posting_id,
        reason="no board token + posting id could be derived from the job row or its URLs",
    )


def _extract_fields(payload: dict) -> list[FormField]:
    fields: list[FormField] = []
    for q in payload.get("questions", []) or []:
        label = q.get("label") or ""
        required = bool(q.get("required"))
        for f in q.get("fields", []) or []:
            name = f.get("name") or label
            ftype = _TYPE_MAP.get(f.get("type"), f.get("type") or "input_text")
            choices = [v.get("label", "") for v in (f.get("values") or [])]
            fields.append(FormField(name=name, label=label, field_type=ftype, required=required, choices=choices))
    return fields


class GreenhouseApplicationProvider(ApplicationProvider):
    name = "greenhouse"
    capabilities = ApplicationCapabilities(
        provider="greenhouse", provider_version="1.1.0",
        form_discovery_supported=True, field_mapping_supported=True,
        draft_fill_supported=True, file_upload_supported=True,
        submission_supported=False, confirmation_detection_supported=False,
        automation_policy=AutomationPolicy.ASSIST_ONLY, support_level=SupportLevel.PARTIAL,
        live_validated=True,
        notes=(
            "Form discovery live-verified against the public boards-api.greenhouse.io "
            "?questions=true Job Board API (real structured fields, including EEOC "
            "demographic questions and, on the posting checked, a sponsorship question). "
            "Real Provider Execution V1 added canonical (board token, posting id) identity, a typed "
            "discovery outcome that tells a permanently-gone posting (404/410) apart from a transient "
            "failure, and the optional check_job_still_active()/classify_job_inactive_reason() hooks -- "
            "all on the SAME documented public read API. Submission via THIS ApplicationProvider (the "
            "ordinary executor pipeline) is still NOT implemented and submission_supported stays False: "
            "Greenhouse's actual apply flow is not a documented public API for programmatic use, so "
            "ASSIST_ONLY per CLAUDE.md Phase 8 section 24. CAPTCHA/auth/confirmation detection is "
            "genuinely impossible on this JSON read API and is honestly left to the browser-assist "
            "layer rather than faked here. Greenhouse Verified Submission Contract V1 separately built a "
            "complete, disabled-by-default real-browser submit engine and canary "
            "(app.applications.greenhouse_submit_engine/greenhouse_canary) that CAN physically click a "
            "real Greenhouse submit control end-to-end -- proven only against local fixtures so far, "
            "never against a real employer. That engine is deliberately NOT wired into this class or the "
            "ordinary AUTO_PERMITTED/approved-submit executor pipeline, and per CLAUDE.md's capability-"
            "honesty rule a local fixture is never sufficient evidence to flip submission_supported here; "
            "only a genuine, explicitly-authorized real-employer canary run could ever justify that."
        ),
    )

    def __init__(self, client: Optional[httpx.Client] = None):
        self._client = client

    # --- identity ---------------------------------------------------------

    def detect_application(self, job: Job) -> bool:
        return canonical_identity(job).recognized

    def canonical_identity(self, job: Job) -> CanonicalIdentity:
        return canonical_identity(job)

    # --- shared public-API access ----------------------------------------

    def _fetch_posting(self, identity: CanonicalIdentity, *, with_questions: bool) -> tuple[Optional[dict], Optional[ProviderHTTPError]]:
        client = self._client or build_client(PROVIDER_HTTP_TIMEOUT_SECONDS)
        owns_client = self._client is None
        url = GREENHOUSE_JOB_URL.format(token=identity.board_token, job_id=identity.posting_id)
        params = {"questions": "true"} if with_questions else None
        try:
            return get_json(client, url, provider="greenhouse-application", params=params), None
        except ProviderHTTPError as exc:
            return None, exc
        finally:
            if owns_client:
                client.close()

    # --- form discovery ---------------------------------------------------

    def discover_form_detailed(self, job: Job) -> FormDiscoveryResult:
        from app.applications.fingerprint import compute_fingerprint

        identity = canonical_identity(job)
        if not identity.recognized:
            return FormDiscoveryResult(FormDiscoveryOutcome.NOT_APPLICABLE, identity=identity,
                                        detail=identity.reason)

        payload, error = self._fetch_posting(identity, with_questions=True)
        if error is not None:
            status = error.status_code
            if status in _GONE_STATUS_CODES:
                logger.info("greenhouse posting %s/%s is gone (HTTP %s)",
                            identity.board_token, identity.posting_id, status)
                return FormDiscoveryResult(FormDiscoveryOutcome.JOB_GONE, identity=identity, status_code=status,
                                            detail=f"public Job Board API returned HTTP {status} for this posting")
            if status in (401, 403):
                return FormDiscoveryResult(FormDiscoveryOutcome.ACCESS_REFUSED, identity=identity,
                                            status_code=status,
                                            detail=f"public Job Board API refused access (HTTP {status})")
            logger.warning("greenhouse application form discovery failed for job %s: %s", job.id, error)
            return FormDiscoveryResult(FormDiscoveryOutcome.TEMPORARY_FAILURE, identity=identity,
                                        status_code=status, detail=str(error)[:300])

        fields = _extract_fields(payload or {})
        if not fields:
            return FormDiscoveryResult(FormDiscoveryOutcome.NO_QUESTIONS_EXPOSED, identity=identity,
                                        detail="the board returned no structured application questions")
        snap = FormSnapshot(
            provider="greenhouse", tenant_identifier=identity.board_token,
            external_job_id=identity.posting_id, fields=fields,
        )
        snap.fingerprint = compute_fingerprint(snap)
        return FormDiscoveryResult(FormDiscoveryOutcome.DISCOVERED, form=snap, identity=identity)

    def discover_form(self, job: Job) -> Optional[FormSnapshot]:
        """Unchanged ApplicationProvider contract: a FormSnapshot, or None.
        `discover_form_detailed()` is the richer variant."""
        return self.discover_form_detailed(job).form

    def normalized_form(self, job: Job, application_fields: list[ApplicationField]) -> Optional[NormalizedForm]:
        """Provider-neutral projection (app.applications.form_model) of the
        discovered schema -- the shape the pre-submit manifest reads."""
        form = self.discover_form(job)
        if form is None:
            return None
        return normalize_form_snapshot(form, application_fields, source=FormFieldSource.PROVIDER_API)

    # --- liveness ---------------------------------------------------------

    def check_job_still_active(self, job: Job) -> Optional[bool]:
        """CLAUDE.md Phase 9 section 25's OPTIONAL hook. Returns False ONLY
        on genuine permanent evidence (404/410 from the public API), True on
        a successful lookup, and None ("not checkable") for anything else --
        a timeout, a 5xx, or a 403 must never be mistaken for an expired
        posting."""
        identity = canonical_identity(job)
        if not identity.recognized:
            return None
        _payload, error = self._fetch_posting(identity, with_questions=False)
        if error is None:
            return True
        if error.status_code in _GONE_STATUS_CODES:
            return False
        return None

    def classify_job_inactive_reason(self, job: Job) -> Optional[str]:
        """Only ever distinguishes what the API genuinely tells us: HTTP 410
        Gone is an explicit "this existed and is now permanently gone"
        (EXPIRED); HTTP 404 is "no such posting" (REMOVED). Anything else
        returns None so the caller falls back to its generic terminal
        blocker rather than inventing a reason."""
        identity = canonical_identity(job)
        if not identity.recognized:
            return None
        _payload, error = self._fetch_posting(identity, with_questions=False)
        if error is None:
            return None
        if error.status_code == 410:
            return "EXPIRED"
        if error.status_code == 404:
            return "REMOVED"
        return None

    # --- mapping / fill / validation (unchanged Phase 8 behavior) ---------

    def map_fields(self, form: FormSnapshot, application_fields) -> MappingResult:
        mapped: list[MappedField] = []
        unmapped_required: list[FormField] = []
        for ff in form.fields:
            field_id, confidence = match_field(ff.label, ff.name)
            app_field = find_field(application_fields, field_id) if field_id else None
            mapped.append(MappedField(form_field=ff, application_field=app_field, confidence=confidence))
            if ff.required and app_field is None:
                unmapped_required.append(ff)
        return MappingResult(mapped=mapped, unmapped_required=unmapped_required)

    def fill_draft(self, form: FormSnapshot, mapping: MappingResult) -> DraftResult:
        filled: list[str] = []
        unresolved: list[str] = []
        uploads: list[str] = []
        for m in mapping.mapped:
            af = m.application_field
            if af is None:
                if m.form_field.required:
                    unresolved.append(m.form_field.name)
                continue
            if not af.auto_fill_allowed:
                if af.category.value == "DEMOGRAPHICS" and m.form_field.choices:
                    decline = next(
                        (c for c in m.form_field.choices
                         if any(p in c.lower().replace("'", "") for p in DECLINE_TO_SELF_IDENTIFY_PHRASES)),
                        None,
                    )
                    if decline:
                        m.fill_value, m.will_fill, m.confidence = decline, True, FieldConfidence.HIGH
                        filled.append(m.form_field.name)
                        continue
                if m.form_field.required:
                    unresolved.append(m.form_field.name)
                continue
            if m.form_field.field_type == "input_file":
                if af.verified_value:
                    m.fill_value, m.will_fill = af.verified_value, True
                    uploads.append(m.form_field.name)
                    filled.append(m.form_field.name)
                elif m.form_field.required:
                    unresolved.append(m.form_field.name)
                continue
            value = af.verified_value
            if m.form_field.choices and value not in m.form_field.choices:
                if m.form_field.required:
                    unresolved.append(m.form_field.name)
                continue
            m.fill_value, m.will_fill = value, True
            filled.append(m.form_field.name)
        return DraftResult(mapping=mapping, filled_field_ids=filled, unresolved_field_ids=unresolved,
                            file_uploads_ready=uploads)

    def validate(self, job: Job, form: FormSnapshot, draft: DraftResult) -> ValidationResult:
        # ASSIST_ONLY unconditionally -- see class docstring. Still reports
        # exactly what remains unresolved so the human review queue is useful.
        detail = [f"Unresolved field: '{name}'" for name in draft.unresolved_field_ids]
        return ValidationResult(
            ok=len(draft.unresolved_field_ids) == 0,
            policy=AutomationPolicy.ASSIST_ONLY,
            policy_reasons=[PolicyReason.SUBMISSION_INTERFACE_UNSUPPORTED],
            detail=detail or ["Draft fully prepared -- submission requires the candidate to complete it manually."],
        )
