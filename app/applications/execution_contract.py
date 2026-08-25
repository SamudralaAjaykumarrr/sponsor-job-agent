"""Provider EXECUTION contract (Real Provider Execution V1).

The brief's PROVIDER CONTRACT requirement, verbatim:

    Define or strengthen a common provider execution contract separating:
      discovery_supported / form_discovery_supported / fill_supported /
      upload_supported / assist_supported / submission_supported /
      confirmation_supported
    Do NOT collapse these into one boolean.
    Browser fill capability is NOT submission capability.

Before this module, those seven facts genuinely existed but were scattered
across three registries that no single caller read together:

  1. `app.providers.capabilities.ProviderCapabilities` (DISCOVERY -- can we
     find this provider's postings at all).
  2. `app.applications.models.ApplicationCapabilities` (the Phase 8
     network-API application adapters -- form schema, mapping, draft fill,
     file upload, submission, confirmation).
  3. `app.applications.browser_capability_matrix` (the Phase 10-13 real-
     browser ASSIST engine -- what has genuinely been observed working
     against each provider's real rendered form).

This module is a strictly DERIVED, read-only projection over those three. It
owns no facts of its own and can therefore never inflate one: every flag
below names the exact source field it came from, and
`app.applications.doctor._check_execution_contract_consistency` statically
re-derives the contract and fails if any flag disagrees with its source.

The two rules that make this safe:

  - `submission_supported` is sourced from `ApplicationCapabilities
    .submission_supported` ALONE. It is never OR-ed with, upgraded by, or
    inferred from any browser/assist capability. Browser fill capability is
    NOT submission capability -- that is the single most important line in
    the brief, and it is enforced structurally here (see
    `_submission_supported`, which reads exactly one field) rather than by
    convention. `app.applications.browser_runtime` contains no code path
    that clicks a final submit control at all, and
    `app.applications.doctor._check_no_browser_auto_submit_capability`
    already statically enforces that.
  - Every "either source counts" flag (form discovery / fill / upload /
    confirmation) carries an explicit `*_source` string, so a reader is
    never left guessing whether "True" means a published provider API or a
    live DOM observation. A capability proven only against a local fixture
    is reported as such, never as a live-verified one.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from app.applications.browser_capability_matrix import (
    BrowserVerification,
    ConfirmationCaptureLevel,
    all_rows as browser_rows,
)
from app.applications.provider_registry import all_application_capabilities
from app.providers.registry import all_provider_names, get_capabilities as discovery_capabilities

# The providers the brief's CAPABILITY AUDIT section requires an explicit,
# per-provider statement for. Kept as an ordered tuple so the audit output is
# deterministic; every one of these is genuinely present in at least one of
# the three source registries.
AUDIT_PROVIDERS: tuple[str, ...] = (
    "mock_ats", "greenhouse", "lever", "ashby", "workday", "smartrecruiters", "workable",
)


class CapabilitySource(str, Enum):
    """Where a True flag's evidence genuinely came from."""
    PROVIDER_API = "PROVIDER_API"                # the provider's own published interface
    BROWSER_LIVE_VERIFIED = "BROWSER_LIVE_VERIFIED"    # real browser, real provider page, observed working
    BROWSER_FIXTURE_ONLY = "BROWSER_FIXTURE_ONLY"      # real browser, local deterministic fixture only
    MOCK_FIXTURE = "MOCK_FIXTURE"                # the deterministic in-process mock ATS
    NONE = "NONE"                                # capability not available by any path


def _browser_source(verification: str) -> CapabilitySource:
    if verification == BrowserVerification.LIVE_FORM_VERIFIED.value:
        return CapabilitySource.BROWSER_LIVE_VERIFIED
    if verification == BrowserVerification.FIXTURE_ONLY.value:
        return CapabilitySource.BROWSER_FIXTURE_ONLY
    return CapabilitySource.NONE


@dataclass(frozen=True)
class ProviderExecutionContract:
    """One provider's full execution capability picture. Seven independent
    booleans -- deliberately never collapsed into a single "supported"
    flag."""
    provider: str

    discovery_supported: bool
    form_discovery_supported: bool
    fill_supported: bool
    upload_supported: bool
    assist_supported: bool
    submission_supported: bool
    confirmation_supported: bool

    form_discovery_source: CapabilitySource = CapabilitySource.NONE
    fill_source: CapabilitySource = CapabilitySource.NONE
    upload_source: CapabilitySource = CapabilitySource.NONE
    assist_source: CapabilitySource = CapabilitySource.NONE
    confirmation_source: CapabilitySource = CapabilitySource.NONE
    submission_source: CapabilitySource = CapabilitySource.NONE

    automation_policy: str = "UNSUPPORTED"
    support_level: str = "UNSUPPORTED"
    has_application_adapter: bool = False
    submission_evidence: str = ""
    confirmation_evidence: str = ""
    notes: str = ""

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "discovery_supported": self.discovery_supported,
            "form_discovery_supported": self.form_discovery_supported,
            "fill_supported": self.fill_supported,
            "upload_supported": self.upload_supported,
            "assist_supported": self.assist_supported,
            "submission_supported": self.submission_supported,
            "confirmation_supported": self.confirmation_supported,
            "form_discovery_source": self.form_discovery_source.value,
            "fill_source": self.fill_source.value,
            "upload_source": self.upload_source.value,
            "assist_source": self.assist_source.value,
            "confirmation_source": self.confirmation_source.value,
            "submission_source": self.submission_source.value,
            "automation_policy": self.automation_policy,
            "support_level": self.support_level,
            "has_application_adapter": self.has_application_adapter,
            "submission_evidence": self.submission_evidence,
            "confirmation_evidence": self.confirmation_evidence,
            "notes": self.notes,
        }


def _application_capabilities_by_provider() -> dict[str, dict]:
    """Only rows for a provider with its OWN dedicated ApplicationProvider
    adapter. The single 'generic' row is deliberately excluded: it stands in
    for many providers at once, so attributing its (all-False) capabilities
    to a named provider would be misleading in the opposite direction -- a
    provider with no adapter simply has no PROVIDER_API application
    capability, which is exactly what this function's absence expresses."""
    return {row["provider"]: row for row in all_application_capabilities() if row["provider"] != "generic"}


def _browser_rows_by_provider() -> dict[str, dict]:
    return {row["provider"]: row for row in browser_rows()}


def _generic_row() -> dict:
    """The single `generic` capability row -- what
    `app.applications.provider_registry.get_application_provider()` actually
    hands back for any provider with no dedicated adapter. Used ONLY to
    report the effective automation_policy/support_level for such a
    provider (reporting a bare "UNSUPPORTED" there would understate what the
    product really does: it still prepares an ASSIST-only draft). Never used
    for any of the seven capability flags."""
    return next(
        (row for row in all_application_capabilities() if row["provider"] == "generic"),
        {"automation_policy": "UNSUPPORTED", "support_level": "UNSUPPORTED", "notes": ""},
    )


def _submission_supported(app_caps: Optional[dict]) -> tuple[bool, CapabilitySource, str]:
    """Reads EXACTLY ONE field. Never OR-ed with any browser/assist
    capability -- see this module's docstring."""
    if app_caps is None:
        return False, CapabilitySource.NONE, (
            "no dedicated ApplicationProvider adapter exists for this provider, so there is no tested "
            "submission interface of any kind"
        )
    if not app_caps["submission_supported"]:
        return False, CapabilitySource.NONE, (
            "ApplicationCapabilities.submission_supported is False -- no genuinely tested, explicitly permitted "
            "end-to-end submission interface exists for this provider"
        )
    source = (
        CapabilitySource.MOCK_FIXTURE if app_caps["provider"] == "mock_ats" else CapabilitySource.PROVIDER_API
    )
    return True, source, "ApplicationCapabilities.submission_supported is True"


def build_contract(provider: str) -> ProviderExecutionContract:
    provider = (provider or "").lower()
    app_caps = _application_capabilities_by_provider().get(provider)
    browser = _browser_rows_by_provider().get(provider)
    discovery = discovery_capabilities(provider)

    browser_verification = browser["verification"] if browser else BrowserVerification.NOT_TESTED.value
    browser_src = _browser_source(browser_verification)
    browser_usable = browser is not None and browser_src != CapabilitySource.NONE

    # --- 1. discovery -----------------------------------------------------
    discovery_supported = bool(discovery and discovery.discovery_supported)

    # --- 2. form discovery ------------------------------------------------
    api_form = bool(app_caps and app_caps["form_discovery_supported"])
    dom_form = bool(browser_usable and browser["field_discovery"])
    if api_form:
        form_source = CapabilitySource.MOCK_FIXTURE if provider == "mock_ats" else CapabilitySource.PROVIDER_API
    elif dom_form:
        form_source = browser_src
    else:
        form_source = CapabilitySource.NONE

    # --- 3. fill ----------------------------------------------------------
    api_fill = bool(app_caps and app_caps["draft_fill_supported"])
    dom_fill = bool(browser_usable and browser["safe_autofill"])
    if api_fill:
        fill_source = CapabilitySource.MOCK_FIXTURE if provider == "mock_ats" else CapabilitySource.PROVIDER_API
    elif dom_fill:
        fill_source = browser_src
    else:
        fill_source = CapabilitySource.NONE

    # --- 4. upload --------------------------------------------------------
    api_upload = bool(app_caps and app_caps["file_upload_supported"])
    dom_upload = bool(browser_usable and browser["resume_upload"])
    if api_upload:
        upload_source = CapabilitySource.MOCK_FIXTURE if provider == "mock_ats" else CapabilitySource.PROVIDER_API
    elif dom_upload:
        upload_source = browser_src
    else:
        upload_source = CapabilitySource.NONE

    # --- 5. assist (real-browser ASSIST engine) ---------------------------
    assist_supported = bool(browser_usable and browser["field_discovery"])
    assist_source = browser_src if assist_supported else CapabilitySource.NONE

    # --- 6. submission (single-source, never inferred) --------------------
    submission_supported, submission_source, submission_evidence = _submission_supported(app_caps)

    # --- 7. confirmation --------------------------------------------------
    api_confirmation = bool(app_caps and app_caps["confirmation_detection_supported"])
    capture_level = (
        browser["confirmation_capture_level"] if browser else ConfirmationCaptureLevel.NOT_OBSERVED.value
    )
    dom_confirmation = capture_level != ConfirmationCaptureLevel.NOT_OBSERVED.value
    confirmation_supported = api_confirmation or dom_confirmation
    if api_confirmation:
        confirmation_source = (
            CapabilitySource.MOCK_FIXTURE if provider == "mock_ats" else CapabilitySource.PROVIDER_API
        )
        confirmation_evidence = "the provider adapter's own verify_confirmation() is genuinely implemented"
    elif dom_confirmation:
        confirmation_source = (
            CapabilitySource.MOCK_FIXTURE if provider == "mock_ats" else CapabilitySource.BROWSER_FIXTURE_ONLY
            if capture_level == ConfirmationCaptureLevel.FIXTURE_VERIFIED.value else browser_src
        )
        confirmation_evidence = (
            "browser-assist confirmation capture recorded as "
            f"{capture_level} in app.applications.browser_capability_matrix -- the parser itself is "
            "app.applications.confirmation_parser, graded by app.applications.confirmation_evidence"
        )
    else:
        confirmation_source = CapabilitySource.NONE
        confirmation_evidence = "no confirmation capture has been genuinely observed for this provider"

    return ProviderExecutionContract(
        provider=provider,
        discovery_supported=discovery_supported,
        form_discovery_supported=api_form or dom_form,
        fill_supported=api_fill or dom_fill,
        upload_supported=api_upload or dom_upload,
        assist_supported=assist_supported,
        submission_supported=submission_supported,
        confirmation_supported=confirmation_supported,
        form_discovery_source=form_source, fill_source=fill_source, upload_source=upload_source,
        assist_source=assist_source, confirmation_source=confirmation_source,
        submission_source=submission_source,
        automation_policy=(app_caps or _generic_row()).get("automation_policy", "UNSUPPORTED"),
        support_level=(app_caps or _generic_row()).get("support_level", "UNSUPPORTED"),
        has_application_adapter=app_caps is not None,
        submission_evidence=submission_evidence,
        confirmation_evidence=confirmation_evidence,
        notes=(app_caps or _generic_row()).get("notes", ""),
    )


def all_contracts() -> list[ProviderExecutionContract]:
    """Every provider named in ANY of the three source registries, plus the
    brief's audit list -- so a provider that exists only as a discovery
    connector still gets an honest, all-False execution row rather than
    silently disappearing."""
    names = set(AUDIT_PROVIDERS)
    names.update(all_provider_names())
    names.update(_application_capabilities_by_provider().keys())
    names.update(_browser_rows_by_provider().keys())
    return [build_contract(name) for name in sorted(names)]


def audit_contracts() -> list[ProviderExecutionContract]:
    """Exactly the providers the brief's CAPABILITY AUDIT names, in its
    order."""
    return [build_contract(name) for name in AUDIT_PROVIDERS]


_FLAGS = (
    "discovery_supported", "form_discovery_supported", "fill_supported", "upload_supported",
    "assist_supported", "submission_supported", "confirmation_supported",
)


def render_audit(contracts: Optional[list[ProviderExecutionContract]] = None) -> str:
    """The capability audit the brief asks to be printed at the end. Always
    states `submission_supported` and `confirmation_supported` explicitly,
    with the evidence behind each."""
    rows = contracts if contracts is not None else audit_contracts()
    lines = ["Provider Execution Capability Audit", "=" * 70]
    for c in rows:
        d = c.as_dict()
        lines.append(f"\nProvider: {c.provider}")
        for flag in _FLAGS:
            source_key = {
                "form_discovery_supported": "form_discovery_source", "fill_supported": "fill_source",
                "upload_supported": "upload_source", "assist_supported": "assist_source",
                "submission_supported": "submission_source", "confirmation_supported": "confirmation_source",
            }.get(flag)
            suffix = f"  (source: {d[source_key]})" if source_key else ""
            lines.append(f"  {flag:<28} {str(d[flag]):<5}{suffix}")
        adapter = (
            "dedicated ApplicationProvider adapter"
            if c.has_application_adapter else "generic ASSIST-only fallback (no dedicated adapter)"
        )
        lines.append(f"  automation_policy            {c.automation_policy}  ({adapter})")
        lines.append(f"  support_level                {c.support_level}")
        lines.append(f"  submission evidence:         {c.submission_evidence}")
        lines.append(f"  confirmation evidence:       {c.confirmation_evidence}")
    lines.append("")
    lines.append(
        "NOTE: browser fill/assist capability is NEVER submission capability. `submission_supported` is read "
        "from ApplicationCapabilities.submission_supported alone and is True only for the deterministic "
        "in-process mock_ats fixture."
    )
    return "\n".join(lines) + "\n"


def build_matrix() -> dict:
    columns = [
        ("provider", "Provider"),
        ("discovery_supported", "Discovery"),
        ("form_discovery_supported", "Form discovery"),
        ("fill_supported", "Fill"),
        ("upload_supported", "Upload"),
        ("assist_supported", "Browser assist"),
        ("submission_supported", "Automated submission"),
        ("confirmation_supported", "Confirmation detection"),
        ("automation_policy", "Automation policy"),
        ("support_level", "Support level"),
        ("submission_evidence", "Why submission is/isn't supported"),
    ]
    return {"columns": columns, "rows": [c.as_dict() for c in all_contracts()]}
