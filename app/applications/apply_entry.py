"""Provider-neutral pre-form navigation stage (CLAUDE.md Phase 11 sections
4-7, 31-32). Some real ATS postings (SmartRecruiters observed this way in
Phase 10; Workday's account/login gate is the same shape) expose a
job-description LANDING PAGE first -- the real application form only exists
behind a further "Apply"/"Apply Now"/"Start Application" click. This module
is pure, deterministic, dependency-free classification logic (no Playwright
import) so it can be unit-tested without a browser and reused identically by
app.applications.browser_runtime (the only module that actually drives a
real page).

Safety invariant this module exists to enforce (CLAUDE.md Phase 11 section
5-6): clicking a control is only ever safe when it is a REVERSIBLE
navigation that begins/opens the application form. A control whose text
reads like a final, irreversible submission (`Submit Application`, `Send
Application`, `Complete Application`) must classify as FINAL_SUBMIT, never
NAVIGATION_SAFE -- regardless of how similar its surrounding page looks to
an apply-entry landing page. `app.applications.browser_runtime` never calls
`.click()` on anything this module doesn't classify NAVIGATION_SAFE, and
FINAL_SUBMIT is never clicked by any code path in this project."""

from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse


class EntryStage(str, Enum):
    """Where a real ATS page currently sits in the apply flow, CLAUDE.md
    Phase 11 section 4. Set on `DiscoveryOutcome.stage` /
    `browser_assist_sessions.stage`."""
    LANDING_PAGE = "LANDING_PAGE"
    APPLICATION_ENTRY = "APPLICATION_ENTRY"
    APPLICATION_FORM = "APPLICATION_FORM"
    FINAL_REVIEW = "FINAL_REVIEW"
    CONFIRMATION = "CONFIRMATION"


class ApplyControlClassification(str, Enum):
    """CLAUDE.md Phase 11 section 6: deterministic classification of any
    button/link found on a real ATS page. Only NAVIGATION_SAFE may ever be
    clicked by app.applications.browser_runtime, and only during the
    pre-form entry stage."""
    NAVIGATION_SAFE = "NAVIGATION_SAFE"
    FINAL_SUBMIT = "FINAL_SUBMIT"
    LOGIN_TRIGGER = "LOGIN_TRIGGER"
    EXTERNAL_REDIRECT = "EXTERNAL_REDIRECT"
    UNKNOWN = "UNKNOWN"


class EntryDetectionResult(str, Enum):
    """CLAUDE.md Phase 11 section 31: the normalized result of one
    apply-entry navigation attempt."""
    ENTRY_READY = "ENTRY_READY"
    FORM_ALREADY_VISIBLE = "FORM_ALREADY_VISIBLE"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    REDIRECT_REQUIRED = "REDIRECT_REQUIRED"
    USER_ACTION_REQUIRED = "USER_ACTION_REQUIRED"
    UNSUPPORTED = "UNSUPPORTED"


class StepConfidence(str, Enum):
    """CLAUDE.md Phase 11 section 19: never invent total_steps -- only ever
    EXACT (a genuinely parsed 'Step X of Y'/progress indicator),
    INFERRED (a same-session heuristic, e.g. "we saw a Next button so there
    is at least one more step"), or UNKNOWN."""
    EXACT = "EXACT"
    INFERRED = "INFERRED"
    UNKNOWN = "UNKNOWN"


class NavControlKind(str, Enum):
    """CLAUDE.md Phase 11 section 20: multi-step navigation safety --
    distinct from ApplyControlClassification, which is about the
    pre-form entry step specifically. This is about controls found ON an
    in-progress multi-step FORM."""
    NEXT_SAFE = "NEXT_SAFE"
    BACK_SAFE = "BACK_SAFE"
    SAVE_CONTINUE = "SAVE_CONTINUE"
    FINAL_SUBMIT = "FINAL_SUBMIT"
    UNKNOWN = "UNKNOWN"


# --- Phrase tables -----------------------------------------------------------
# Deliberately disjoint: a phrase in one tuple never also appears in another,
# so classification is a single unambiguous lookup, never a priority
# tie-break between two matching categories. "apply now" was previously
# (Phase 10) misclassified as a FINAL-submit phrase -- a real bug this phase
# fixed (see docs/phase11-ats-flow-hardening.md "bugs caught").
NAVIGATION_SAFE_PHRASES = (
    "apply now", "apply for this job", "apply for this position", "apply to this job",
    "start application", "start your application", "continue application",
    "continue your application", "begin application", "apply here", "apply",
)
FINAL_SUBMIT_PHRASES = (
    "submit application", "submit your application", "send application",
    "complete application", "finish application", "submit", "send",
)
LOGIN_TRIGGER_PHRASES = (
    "sign in to apply", "log in to apply", "login to apply", "create an account",
    "create account", "sign up to apply", "sign in", "log in", "login",
)
BACK_PHRASES = ("back", "previous", "go back")
SAVE_CONTINUE_PHRASES = ("save and continue", "save and continue later", "save for later")
NEXT_PHRASES = ("next", "continue", "next step")

_REVIEW_PAGE_PHRASES = (
    "review your application", "review application", "final review", "review and submit",
    "please review", "review your answers",
)
_CONFIRMATION_PAGE_PHRASES = (
    "thank you for applying", "application received", "application submitted", "successfully applied",
    "we've received your application", "we have received your application",
    "your application has been submitted", "thank you for your application",
)


def _norm(text: str) -> str:
    return (text or "").strip().lower()


def classify_apply_control(
    text: str, *, href: str = "", current_host: str = "",
) -> ApplyControlClassification:
    """Deterministic, order-independent classification of one button/link's
    visible text (plus its destination host, when known -- an <a href> to an
    unrelated external host is EXTERNAL_REDIRECT regardless of its text).
    UNKNOWN (never a guess dressed up as a class) is the correct result for
    text this table doesn't recognize -- callers must treat UNKNOWN as
    USER_ACTION_REQUIRED, never as safe to click (CLAUDE.md Phase 11 section
    6)."""
    label = _norm(text)
    if not label:
        return ApplyControlClassification.UNKNOWN

    if href:
        dest_host = (urlparse(href).hostname or "").lower()
        if dest_host and current_host and dest_host != current_host:
            return ApplyControlClassification.EXTERNAL_REDIRECT

    # FINAL_SUBMIT checked before NAVIGATION_SAFE: "submit application"
    # contains no navigation-safe phrase, but a short label like "submit"
    # must never fall through to a partial "apply"-style match.
    if any(p in label for p in FINAL_SUBMIT_PHRASES):
        return ApplyControlClassification.FINAL_SUBMIT
    if any(p in label for p in LOGIN_TRIGGER_PHRASES):
        return ApplyControlClassification.LOGIN_TRIGGER
    if any(p in label for p in NAVIGATION_SAFE_PHRASES):
        return ApplyControlClassification.NAVIGATION_SAFE
    return ApplyControlClassification.UNKNOWN


def classify_nav_control(text: str) -> NavControlKind:
    """CLAUDE.md Phase 11 section 20: classifies a control found ON a
    multi-step form page (not the pre-form entry landing page) -- keeps
    'Next' cleanly distinguishable from a final submit action, and from
    'Back'/'Save & Continue', so advance_step() and any future back/save
    action never risk clicking the wrong kind of control."""
    label = _norm(text)
    if not label:
        return NavControlKind.UNKNOWN
    if any(p in label for p in FINAL_SUBMIT_PHRASES):
        return NavControlKind.FINAL_SUBMIT
    if any(p in label for p in SAVE_CONTINUE_PHRASES):
        return NavControlKind.SAVE_CONTINUE
    if any(p in label for p in BACK_PHRASES):
        return NavControlKind.BACK_SAFE
    if any(p in label for p in NEXT_PHRASES):
        return NavControlKind.NEXT_SAFE
    return NavControlKind.UNKNOWN


def detect_entry_result(
    *, has_apply_control: bool, apply_control_classification: ApplyControlClassification | None,
    has_form_fields: bool, login_wall_present: bool,
) -> EntryDetectionResult:
    """CLAUDE.md Phase 11 section 31: normalizes one page's apply-entry
    situation into a single result the orchestration layer switches on.
    Pure function over already-observed booleans -- callers
    (app.applications.browser_runtime) supply the real DOM observations."""
    if login_wall_present:
        return EntryDetectionResult.LOGIN_REQUIRED
    if has_form_fields:
        return EntryDetectionResult.FORM_ALREADY_VISIBLE
    if has_apply_control:
        if apply_control_classification == ApplyControlClassification.NAVIGATION_SAFE:
            return EntryDetectionResult.ENTRY_READY
        if apply_control_classification == ApplyControlClassification.LOGIN_TRIGGER:
            return EntryDetectionResult.LOGIN_REQUIRED
        if apply_control_classification == ApplyControlClassification.EXTERNAL_REDIRECT:
            return EntryDetectionResult.REDIRECT_REQUIRED
        return EntryDetectionResult.USER_ACTION_REQUIRED
    return EntryDetectionResult.UNSUPPORTED


def classify_stage(
    *, has_form_fields: bool, has_apply_control: bool, is_review_page: bool, is_confirmation_page: bool,
) -> EntryStage:
    """CLAUDE.md Phase 11 section 4. Order matters: confirmation and review
    are checked before form-fields/apply-control, since a review page can
    legitimately still contain a handful of read-only-looking inputs."""
    if is_confirmation_page:
        return EntryStage.CONFIRMATION
    if is_review_page:
        return EntryStage.FINAL_REVIEW
    if has_form_fields:
        return EntryStage.APPLICATION_FORM
    if has_apply_control:
        return EntryStage.LANDING_PAGE
    return EntryStage.APPLICATION_ENTRY


def is_review_page_text(body_text: str) -> bool:
    lowered = _norm(body_text)
    return any(p in lowered for p in _REVIEW_PAGE_PHRASES)


def is_confirmation_page_text(body_text: str) -> bool:
    lowered = _norm(body_text)
    return any(p in lowered for p in _CONFIRMATION_PAGE_PHRASES)


_STEP_OF_RE = None
_STEP_SLASH_RE = None


def parse_step_progress(body_text: str) -> tuple[int | None, int | None, StepConfidence]:
    """CLAUDE.md Phase 11 sections 18-19: extracts a genuinely-displayed
    step/progress indicator from page text -- "Step 2 of 4", "Progress: 3 /
    5", etc. Returns (current_step, total_steps, confidence). Never invents
    total_steps: a bare "Step 2" with no total anywhere yields
    (2, None, INFERRED); no recognizable pattern yields (None, None,
    UNKNOWN).

    A real live-Chromium run against GitLab's genuine Greenhouse posting
    (Phase 11's own validation, see scripts/phase11_live_validation.py)
    caught a real bug here: an earlier ungated `\\d{1,2}\\s*/\\s*\\d{1,2}`
    pattern matched an unrelated date ("7/31") elsewhere on the page as if
    it were "step 7 of 31". The slash form now REQUIRES a "step"/"progress"
    keyword within a short window before the numbers, exactly like the
    "step N of M" form already did -- a bare ratio anywhere in the page
    text is never trusted."""
    import re

    global _STEP_OF_RE, _STEP_SLASH_RE
    if _STEP_OF_RE is None:
        _STEP_OF_RE = re.compile(r"step\s+(\d+)\s+of\s+(\d+)", re.I)
        _STEP_SLASH_RE = re.compile(r"(?:step|progress)[^\d]{0,20}?(\d{1,2})\s*/\s*(\d{1,2})", re.I)
    text = body_text or ""

    match = _STEP_OF_RE.search(text)
    if match:
        return int(match.group(1)), int(match.group(2)), StepConfidence.EXACT

    match = _STEP_SLASH_RE.search(text)
    if match:
        current, total = int(match.group(1)), int(match.group(2))
        if 0 < current <= total:
            return current, total, StepConfidence.EXACT

    bare_step = re.search(r"\bstep\s+(\d+)\b", text, re.I)
    if bare_step:
        return int(bare_step.group(1)), None, StepConfidence.INFERRED

    return None, None, StepConfidence.UNKNOWN


@dataclass
class EntryNavigationOutcome:
    """What one apply-entry navigation attempt produced -- distinct from
    browser_runtime.DiscoveryOutcome (which is post-navigation form state);
    this is the pre-form decision that determines whether a click happens at
    all."""
    result: EntryDetectionResult
    stage: EntryStage
    control_classification: ApplyControlClassification | None = None
    control_text: str = ""
    clicked: bool = False
    reason: str = ""
