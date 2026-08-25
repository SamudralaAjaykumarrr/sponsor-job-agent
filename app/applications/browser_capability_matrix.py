"""CLAUDE.md Phase 10 sections 26-27, 59: the browser-assist capability
matrix. Deliberately a SEPARATE, small, data-only registry from
app.applications.provider_registry/capability_matrix (which describe the
Phase 8 network-API-based ApplicationProvider adapters) -- see the module
docstring rationale below for why no `RealATSAssistProvider` class hierarchy
was built to replace it.

Why data, not a class hierarchy: app.applications.browser_runtime's DOM
scan/fill/detect engine is genuinely PROVIDER-AGNOSTIC by construction --
it discovers real fields from whatever HTML a real browser renders, the
same way for Greenhouse, Lever, Ashby, or any other ATS, with no
per-provider subclass needed (proven live this phase against real
Greenhouse/Lever/Ashby/SmartRecruiters pages -- see
docs/real-ats-validation.md). Forcing a parallel `ApplicationProvider`-style
class hierarchy on top of that would only reintroduce per-provider branching
the generic engine deliberately avoids. What DOES vary per provider is
purely descriptive: has this specific provider's real candidate-facing form
actually been opened and inspected live this phase, or only exercised
against a local fixture, or not attempted at all. That is exactly what this
module tracks -- never a capability claim about automation support (that
remains app.applications.provider_registry's job).

Every row here must be updated only from a genuine, dated observation (a
live browser open + `app.applications.browser_runtime._detect_fields()`
result, or an honest "not attempted") -- never inflated."""

from dataclasses import dataclass
from enum import Enum


class BrowserVerification(str, Enum):
    LIVE_FORM_VERIFIED = "LIVE_FORM_VERIFIED"
    FIXTURE_ONLY = "FIXTURE_ONLY"
    NOT_TESTED = "NOT_TESTED"


class ConfirmationCaptureLevel(str, Enum):
    """Real Provider Execution V1: a STRUCTURED restatement of the level of
    confirmation-capture evidence each row's free-text `confirmation_capture`
    field already recorded, so `app.applications.execution_contract` can
    answer "is confirmation_supported true for this provider?" from data
    rather than by prose-matching. Every value below was read off the
    EXISTING dated prose in this module -- no row's evidence level was
    raised, and none may ever be raised without a fresh genuine observation.

    LIVE_SUBMISSION_VERIFIED deliberately exists but is used by NO row and
    reachable by no current code path: proving it would require genuinely
    submitting a real application to a real employer, which this project
    never does. It is modeled so the vocabulary can express the distinction
    honestly rather than letting FIXTURE_VERIFIED quietly stand in for it."""
    LIVE_SUBMISSION_VERIFIED = "LIVE_SUBMISSION_VERIFIED"
    FIXTURE_VERIFIED = "FIXTURE_VERIFIED"
    NOT_OBSERVED = "NOT_OBSERVED"


@dataclass(frozen=True)
class BrowserCapabilityRow:
    provider: str
    verification: BrowserVerification
    field_discovery: bool
    safe_autofill: bool
    resume_upload: bool
    multi_step: str  # "verified" | "generic engine, not yet exercised on this provider" | "unknown"
    login_handoff: str
    captcha_handoff: str
    final_submit_automation: bool
    confirmation_capture: str
    notes: str
    confirmation_capture_level: ConfirmationCaptureLevel = ConfirmationCaptureLevel.NOT_OBSERVED

    def as_dict(self) -> dict:
        return {
            "provider": self.provider, "verification": self.verification.value,
            "field_discovery": self.field_discovery, "safe_autofill": self.safe_autofill,
            "resume_upload": self.resume_upload, "multi_step": self.multi_step,
            "login_handoff": self.login_handoff, "captcha_handoff": self.captcha_handoff,
            "final_submit_automation": self.final_submit_automation,
            "confirmation_capture": self.confirmation_capture,
            "confirmation_capture_level": self.confirmation_capture_level.value,
            "notes": self.notes,
        }


# Dated: 2026-08-22 (Phase 12's own bounded live validation run,
# scripts/phase12_live_validation.py, plus the local sandbox E2E suite --
# tests/test_browser_assist_e2e.py, tests/test_browser_assist_phase11_e2e.py,
# tests/test_browser_assist_phase12_e2e.py -- real Chromium against
# tests/browser_fixtures.py). Update the row (and this comment's date) the
# next time a provider is genuinely re-checked -- never bump verification
# without a fresh observation.
_ROWS: list[BrowserCapabilityRow] = [
    BrowserCapabilityRow(
        provider="greenhouse", verification=BrowserVerification.LIVE_FORM_VERIFIED,
        field_discovery=True, safe_autofill=True, resume_upload=True,
        multi_step="verified (real local sandbox multi-page fixture; the live GitLab posting checked was a "
                   "single-page apply form)",
        login_handoff="detects `input[type=password]` -- verified on local sandbox fixture, no live posting "
                       "required login",
        captcha_handoff="verified live -- the real GitLab posting genuinely presented a CAPTCHA widget, "
                         "correctly detected and paused, never bypassed",
        final_submit_automation=False,
        confirmation_capture="verified on local sandbox fixture (success-page text + confirmation id regex); "
                              "not exercised against a real submission (never submitted for real)",
        confirmation_capture_level=ConfirmationCaptureLevel.FIXTURE_VERIFIED,
        notes="Live-opened a real GitLab application page every phase since Phase 3: 23-24 real fields detected "
              "including resume upload and a genuine sponsorship question, submit button detected and never "
              "clicked. Phase 11 additionally found and safely CLICKED a real NAVIGATION_SAFE 'Apply' control on "
              "this same posting (apply-first-click genuinely proven end-to-end on a real, unrelated ATS) -- see "
              "docs/apply-entry-navigation.md. A real live run also caught and fixed a step-progress false "
              "positive: an unrelated on-page date ('7/31') was initially misread as 'step 7 of 31' by an early, "
              "too-permissive regex. Phase 12 REGRESSION-CONFIRMED: GitLab's board migrated hosts from "
              "boards.greenhouse.io/gitlab to job-boards.greenhouse.io/gitlab between phases (a real, organic ATS "
              "URL-shape change, not a project bug) -- the generic '.greenhouse.io' domain-allowlist suffix match "
              "and apply-first-click both continued working unchanged against the new host with zero code "
              "changes, 23 fields detected, apply-entry click succeeded. Phase 12 also live-verified (10/10 real "
              "links) that GitLab's own CORPORATE careers page (about.gitlab.com, not a greenhouse.io domain) "
              "linking out to job-boards.greenhouse.io classifies TRUSTED_ATS_REDIRECT -- the first genuine "
              "real-world company-career-page-to-ATS-domain trust proof, see docs/trusted-ats-redirects.md.",
    ),
    BrowserCapabilityRow(
        provider="lever", verification=BrowserVerification.LIVE_FORM_VERIFIED,
        field_discovery=True, safe_autofill=True, resume_upload=True,
        multi_step="unknown -- the live posting checked was single-page",
        login_handoff="verified on local sandbox fixture only",
        captcha_handoff="the live posting genuinely presented a CAPTCHA widget, correctly detected and paused",
        final_submit_automation=False,
        confirmation_capture="verified on local sandbox fixture only",
        confirmation_capture_level=ConfirmationCaptureLevel.FIXTURE_VERIFIED,
        notes="Live-opened a real posting on Lever's own public demo account (api.lever.co/v0/postings/leverdemo): "
              "22 real fields detected (name/email/phone/resume/EEOC demographic questions), submit button "
              "detected and never clicked. This is the SAME real form the Phase 8 providers_lever.py adapter "
              "documented as having no structured API schema -- the browser engine reaches it anyway by reading "
              "the rendered DOM directly, which is exactly the gap browser-assist exists to close. Phase 11/12: "
              "an apply-entry-shaped control is found on this page but classifies EXTERNAL_REDIRECT with "
              "redirect_trust=UNTRUSTED (the destination is neither this page's own host nor a recognized ATS "
              "vendor domain -- Phase 12's trusted-redirect model correctly does NOT reclassify it, confirming "
              "this was never simply an untrusted-host false negative) -- correctly NEVER clicked; the API's "
              "`applyUrl` is already the real form directly, so apply-first-click is genuinely not needed for "
              "this tenant. Phase 12 also observed the hCaptcha widget's own iframe on this page as an "
              "'unexpected host' when the iframe scan runs standalone -- in the real production discovery path "
              "this is moot, since the existing page-content CAPTCHA check runs BEFORE the iframe scan and pauses "
              "the session first (verified by code inspection, see app.applications.browser_runtime._do_discover).",
    ),
    BrowserCapabilityRow(
        provider="ashby", verification=BrowserVerification.LIVE_FORM_VERIFIED,
        field_discovery=True, safe_autofill=True, resume_upload=True,
        multi_step="unknown -- the live posting checked was single-page",
        login_handoff="verified on local sandbox fixture only",
        captcha_handoff="the live posting genuinely presented a CAPTCHA widget, correctly detected and paused",
        final_submit_automation=False,
        confirmation_capture="verified on local sandbox fixture only",
        confirmation_capture_level=ConfirmationCaptureLevel.FIXTURE_VERIFIED,
        notes="Live-opened a real posting on Ashby's own public careers board (api.ashbyhq.com/posting-api/"
              "job-board/ashby): 27-28 real fields detected including free-response questions and demographic "
              "self-identification choices, submit button detected and never clicked. Phase 12 REGRESSION-"
              "CONFIRMED: the API's `applyUrl` now resolves directly to the '/application' form page itself (no "
              "apply-entry control found at all this run, distinct from Phase 11's EXTERNAL_REDIRECT finding on "
              "a different posting) -- the generic DOM-scan engine reached and mapped the form either way, "
              "showing apply-first-click is genuinely posting-shape-dependent for Ashby, never a fixed provider "
              "property.",
    ),
    BrowserCapabilityRow(
        provider="smartrecruiters", verification=BrowserVerification.NOT_TESTED,
        field_discovery=False, safe_autofill=False, resume_upload=False,
        multi_step="unknown", login_handoff="not observed",
        captcha_handoff="not observed (never reached the real form)",
        final_submit_automation=False, confirmation_capture="not observed",
        notes="Phase 12 CONCLUSIVELY CHARACTERIZED (CLAUDE.md Phase 12 section 76 success criterion B) the "
              "SmartRecruiters limitation, using a web-search-discovered NEWER 'oneclick-ui' client-rendered "
              "posting shape (jobs.smartrecruiters.com/oneclick-ui/company/<Company>/publication/<uuid>) distinct "
              "from the classic postingUrl shape Phase 10/11 tried. Opening a real oneclick-ui posting live "
              "encountered a genuine active bot-detection CAPTCHA challenge (a geo.captcha-delivery.com iframe -- "
              "DataDome's CAPTCHA delivery domain) BEFORE any application content rendered: 0 fields, 0 apply "
              "controls, CAPTCHA correctly detected, never bypassed (CLAUDE.md sections 48, 55 -- no anti-bot "
              "circumvention of any kind was attempted). This is a real, structural limitation of this posting "
              "shape for unauthenticated automated access, not a bug in this project's DOM-scanning engine -- see "
              "docs/smartrecruiters-spa-validation.md for the full run. The classic postingUrl shape (Phase 10/11) "
              "was ALSO re-attempted this phase (Visa's public board) but the company's postings API returned no "
              "results this run (honestly reported NOT RUN, not fabricated). Generic SPA-hardening mechanisms "
              "built this phase (bounded DOM-stabilization wait, client-side route detection, trusted-redirect-"
              "aware apply-entry classification, ambiguous-multi-apply-control detection) are all proven working "
              "against a deterministic local SmartRecruiters-shaped SPA fixture (tests/browser_fixtures.py's "
              "smartrecruiters_like_spa_page, exercised end-to-end in "
              "tests/test_browser_assist_phase12_e2e.py) even though the one real posting type reachable this "
              "phase was CAPTCHA-blocked. Remains honestly NOT_TESTED at the real-form level.",
    ),
    BrowserCapabilityRow(
        provider="workday", verification=BrowserVerification.NOT_TESTED,
        field_discovery=False, safe_autofill=False, resume_upload=False,
        multi_step="unknown (per-tenant, see app.applications.workday_tenant -- Phase 11's own Walmart-tenant "
                   "observation found no multi-step evidence, but zero fields were reached, so this is not a "
                   "meaningful negative)",
        login_handoff="inconsistent across two live loads of the SAME real posting this phase (see notes)",
        captcha_handoff="not observed this phase",
        final_submit_automation=False, confirmation_capture="not observed this phase",
        notes="The Phase 3/10 dogfooded tenant (workday.wd5.myworkdayjobs.com/Workday) remains offline. Phase 11 "
              "found a genuinely LIVE public Workday tenant instead (walmart.wd504.myworkdayjobs.com/"
              "WalmartExternal, found via web search) and opened a real posting on it; Phase 12 repeated the "
              "SAME URL 3 MORE times (bounded, 3-second spacing, CLAUDE.md Phase 12 sections 18-21) via "
              "app.applications.workday_tenant.record_attempt -- results: LOGIN_TRIGGER, LOGIN_TRIGGER, "
              "NAVIGATION_SAFE. app.applications.workday_tenant.classify_stability() correctly computes this as "
              "VARIABLE (2/3 consistent), not STABLE and not cherry-picked to the cleaner-looking run -- see "
              "docs/workday-observation-model.md. This CONFIRMS Phase 11's single-pair-of-observations finding "
              "with genuine repeated evidence rather than resolving it either way; the underlying cause (A/B page "
              "variation, timing-dependent hydration, or session/cookie state) remains undetermined -- honestly "
              "reported per CLAUDE.md Phase 12 section 77 ('success means repeated observations clarify behavior; "
              "it may still remain VARIABLE/ASSIST_ONLY'). Per-tenant observations live in "
              "app.applications.workday_tenant, never generalized to a blanket 'Workday supported' claim -- and "
              "app.applications.doctor._check_workday_universal_claim_from_one_tenant statically enforces this "
              "row can never claim LIVE_FORM_VERIFIED without at least one genuinely STABLE tenant behind it. "
              "2026-08-22 browser-assist hardening pass: (1) app.applications.workday_tenant.record_attempt() is "
              "now called automatically from every real _do_discover() pass for a recognized Workday tenant/site "
              "(browser_runtime._LiveSession._record_workday_attempt), not only from bounded manual validation "
              "scripts, so the LOGIN_TRIGGER/NAVIGATION_SAFE variability above will keep accumulating genuine "
              "evidence from ordinary ASSIST usage going forward; (2) a new dynamic-validation-blocked detection "
              "was added to _do_advance_step() (app.applications.dynamic_validation) for Workday-style multi-step "
              "wizards that silently refuse to advance on an empty required field. Neither change was live-"
              "verified against a real Workday posting this pass (system Chromium unavailable in this sandbox --"
              " see docs/workday-smartrecruiters-workable-browser-hardening.md); this row's verification/"
              "multi_step/login_handoff/captcha_handoff/confirmation_capture fields are therefore left UNCHANGED "
              "rather than inflated.",
    ),
    BrowserCapabilityRow(
        provider="workable", verification=BrowserVerification.LIVE_FORM_VERIFIED,
        field_discovery=True, safe_autofill=True, resume_upload=True,
        multi_step="unknown -- the live posting checked was single-page",
        login_handoff="not observed on this posting",
        captcha_handoff="verified live -- the real posting genuinely presented a CAPTCHA widget, correctly "
                         "detected and paused",
        final_submit_automation=False, confirmation_capture="not observed this phase",
        notes="Phase 11 found a genuinely LIVE public Workable tenant ('flosum', apply.workable.com/flosum) via "
              "web search -- Phase 3/10 never located a real tenant. 14 real fields detected (name/email/phone/"
              "address/LinkedIn/salary-expectation/resume upload), submit button detected and never clicked. An "
              "apply-entry-shaped control was also found but classified EXTERNAL_REDIRECT (redirect_trust="
              "UNTRUSTED) -- correctly never clicked; this tenant's `application_url` from the public widget API "
              "is already the real form directly (the page's own JS still performs an ordinary same-tenant "
              "redirect to add the '/flosum/' path segment, unrelated to apply-entry click-through), so "
              "apply-first-click is genuinely not needed for THIS tenant (never generalized to all Workable "
              "accounts). Phase 12 REGRESSION-CONFIRMED: identical 14-field result on a fresh run. "
              "2026-08-22 browser-assist hardening pass: a new local fixture "
              "(tests/browser_fixtures.py::workable_like_multistep_page) exercises the generic multi-step engine "
              "against a Workable-shaped 2-step flow, since the one real tenant reached so far ('flosum') is "
              "single-page -- this row's `multi_step` field stays 'unknown' for the REAL tenant (no new live "
              "evidence this pass; system Chromium unavailable in this sandbox), the fixture only demonstrates "
              "the generic mechanism itself, matching this module's own 'never generalize from a fixture to a "
              "real-tenant claim' rule.",
    ),
    BrowserCapabilityRow(
        provider="bamboohr", verification=BrowserVerification.NOT_TESTED,
        field_discovery=False, safe_autofill=False, resume_upload=False,
        multi_step="unknown", login_handoff="not observed", captcha_handoff="not observed",
        final_submit_automation=False, confirmation_capture="not observed",
        notes="Not opened live this phase (bounded validation scope). The generic DOM-scan engine applies "
              "uniformly the moment a real posting URL is available -- see greenhouse/lever/ashby rows above for "
              "proof the mechanism itself works against real, unrelated ATS platforms.",
    ),
    BrowserCapabilityRow(
        provider="breezy", verification=BrowserVerification.NOT_TESTED,
        field_discovery=False, safe_autofill=False, resume_upload=False,
        multi_step="unknown", login_handoff="not observed", captcha_handoff="not observed",
        final_submit_automation=False, confirmation_capture="not observed",
        notes="Not opened live this phase (bounded validation scope). See bamboohr row's note on the generic "
              "engine.",
    ),
    BrowserCapabilityRow(
        provider="recruitee", verification=BrowserVerification.NOT_TESTED,
        field_discovery=False, safe_autofill=False, resume_upload=False,
        multi_step="unknown", login_handoff="not observed", captcha_handoff="not observed",
        final_submit_automation=False, confirmation_capture="not observed",
        notes="Not opened live this phase (bounded validation scope). See bamboohr row's note on the generic "
              "engine.",
    ),
    BrowserCapabilityRow(
        provider="mock_ats", verification=BrowserVerification.FIXTURE_ONLY,
        field_discovery=True, safe_autofill=True, resume_upload=True,
        multi_step="verified (local fixture only)", login_handoff="verified (local fixture only)",
        captcha_handoff="verified (local fixture only)", final_submit_automation=False,
        confirmation_capture="verified (local fixture only)",
        confirmation_capture_level=ConfirmationCaptureLevel.FIXTURE_VERIFIED,
        notes="Deterministic in-process test fixture only -- never a real ATS.",
    ),
]


def all_rows() -> list[dict]:
    return [r.as_dict() for r in _ROWS]


def build_matrix() -> dict:
    columns = [
        ("provider", "Provider"), ("verification", "Verification"), ("field_discovery", "Field discovery"),
        ("safe_autofill", "Safe autofill"), ("resume_upload", "Resume upload"), ("multi_step", "Multi-step"),
        ("login_handoff", "Login handoff"), ("captcha_handoff", "CAPTCHA handoff"),
        ("final_submit_automation", "Final-submit automation"), ("confirmation_capture", "Confirmation capture"),
        ("confirmation_capture_level", "Confirmation evidence level"), ("notes", "Notes"),
    ]
    return {"columns": columns, "rows": all_rows()}


def render_text() -> str:
    lines = ["Browser-Assist Capability Matrix", "=" * 40]
    for row in all_rows():
        lines.append(f"\nProvider: {row['provider']}")
        lines.append(f"  Verification:            {row['verification']}")
        lines.append(f"  Field discovery:         {row['field_discovery']}")
        lines.append(f"  Safe autofill:           {row['safe_autofill']}")
        lines.append(f"  Resume upload:           {row['resume_upload']}")
        lines.append(f"  Multi-step:              {row['multi_step']}")
        lines.append(f"  Login handoff:           {row['login_handoff']}")
        lines.append(f"  CAPTCHA handoff:         {row['captcha_handoff']}")
        lines.append(f"  Final-submit automation: {row['final_submit_automation']}")
        lines.append(f"  Confirmation capture:    {row['confirmation_capture']}")
        lines.append(f"  Confirmation evidence:   {row['confirmation_capture_level']}")
        lines.append(f"  Notes:                   {row['notes']}")
    return "\n".join(lines) + "\n"
