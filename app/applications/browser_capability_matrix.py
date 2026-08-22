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

from dataclasses import dataclass, field
from enum import Enum


class BrowserVerification(str, Enum):
    LIVE_FORM_VERIFIED = "LIVE_FORM_VERIFIED"
    FIXTURE_ONLY = "FIXTURE_ONLY"
    NOT_TESTED = "NOT_TESTED"


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

    def as_dict(self) -> dict:
        return {
            "provider": self.provider, "verification": self.verification.value,
            "field_discovery": self.field_discovery, "safe_autofill": self.safe_autofill,
            "resume_upload": self.resume_upload, "multi_step": self.multi_step,
            "login_handoff": self.login_handoff, "captcha_handoff": self.captcha_handoff,
            "final_submit_automation": self.final_submit_automation,
            "confirmation_capture": self.confirmation_capture, "notes": self.notes,
        }


# Dated: 2026-08-22, this phase's own live validation run
# (scripts/phase10_live_validation.py) plus the local sandbox E2E suite
# (tests/test_browser_assist_e2e.py, real Chromium against
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
        notes="Live-opened a real GitLab (boards-api.greenhouse.io token 'gitlab') application page this phase: "
              "24 real fields detected including resume upload and a genuine sponsorship question, submit button "
              "detected and never clicked.",
    ),
    BrowserCapabilityRow(
        provider="lever", verification=BrowserVerification.LIVE_FORM_VERIFIED,
        field_discovery=True, safe_autofill=True, resume_upload=True,
        multi_step="unknown -- the live posting checked was single-page",
        login_handoff="verified on local sandbox fixture only",
        captcha_handoff="the live posting genuinely presented a CAPTCHA widget, correctly detected and paused",
        final_submit_automation=False,
        confirmation_capture="verified on local sandbox fixture only",
        notes="Live-opened a real posting on Lever's own public demo account (api.lever.co/v0/postings/leverdemo): "
              "22 real fields detected (name/email/phone/resume/EEOC demographic questions), submit button "
              "detected and never clicked. This is the SAME real form the Phase 8 providers_lever.py adapter "
              "documented as having no structured API schema -- the browser engine reaches it anyway by reading "
              "the rendered DOM directly, which is exactly the gap browser-assist exists to close.",
    ),
    BrowserCapabilityRow(
        provider="ashby", verification=BrowserVerification.LIVE_FORM_VERIFIED,
        field_discovery=True, safe_autofill=True, resume_upload=True,
        multi_step="unknown -- the live posting checked was single-page",
        login_handoff="verified on local sandbox fixture only",
        captcha_handoff="the live posting genuinely presented a CAPTCHA widget, correctly detected and paused",
        final_submit_automation=False,
        confirmation_capture="verified on local sandbox fixture only",
        notes="Live-opened a real posting on Ashby's own public careers board (api.ashbyhq.com/posting-api/"
              "job-board/ashby): 28 real fields detected including free-response questions and demographic "
              "self-identification choices, submit button detected and never clicked.",
    ),
    BrowserCapabilityRow(
        provider="smartrecruiters", verification=BrowserVerification.NOT_TESTED,
        field_discovery=False, safe_autofill=False, resume_upload=False,
        multi_step="unknown", login_handoff="not observed", captcha_handoff="not observed",
        final_submit_automation=False, confirmation_capture="not observed",
        notes="Live-opened a real posting on SmartRecruiters' own board (jobs.smartrecruiters.com/SmartRecruiters/"
              "<id>) this phase: the candidate-facing URL from the postings API is a job-description LANDING "
              "page (only a 'copy link to share' control was detected) -- the actual application FORM sits "
              "behind a separate 'Apply' click this phase's bounded, single-page-per-provider validation policy "
              "did not follow. Honestly NOT_TESTED at the form level; the landing page's own domain/navigation "
              "behavior was observed and is safe.",
    ),
    BrowserCapabilityRow(
        provider="workday", verification=BrowserVerification.NOT_TESTED,
        field_discovery=False, safe_autofill=False, resume_upload=False,
        multi_step="unknown (Workday's real candidate flow is well known to be multi-page + often requires "
                   "account creation/login, per CLAUDE.md Phase 10 section 24 -- not independently re-verified "
                   "live this phase)",
        login_handoff="not observed this phase", captcha_handoff="not observed this phase",
        final_submit_automation=False, confirmation_capture="not observed this phase",
        notes="The Phase 3 dogfooded discovery tenant (workday.wd5.myworkdayjobs.com/Workday) currently "
              "redirects to a Workday maintenance page as of this session's live check -- genuinely unavailable "
              "right now, not a code defect. No other real Workday tenant was substituted (never guess a tenant "
              "URL). Re-check with a real, currently-live tenant before claiming Workday browser-assist support.",
    ),
    BrowserCapabilityRow(
        provider="workable", verification=BrowserVerification.NOT_TESTED,
        field_discovery=False, safe_autofill=False, resume_upload=False,
        multi_step="unknown", login_handoff="not observed", captcha_handoff="not observed",
        final_submit_automation=False, confirmation_capture="not observed",
        notes="No live-verified public Workable tenant is known -- Phase 3's own dogfooding attempt against "
              "guessed tenant names did not resolve to a real account either. Never fabricated a tenant here.",
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
        ("notes", "Notes"),
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
        lines.append(f"  Notes:                   {row['notes']}")
    return "\n".join(lines) + "\n"
