"""Mid-form dynamic-validation-error detection (Workday/SmartRecruiters/
Workable browser-assist hardening, 2026-08-22). Real multi-step ATS wizards
(Workday's in particular) commonly block a Next/Continue click with
client-side validation when a required field is left empty -- the click
still "succeeds" (no exception, no navigation error) but the page silently
stays on the same step. Before this module existed,
`app.applications.browser_runtime._do_advance_step()` had no way to tell
that apart from a genuine advance and unconditionally reported
`advanced: True`.

Pure, dependency-free classification logic (no Playwright import), matching
`app.applications.apply_entry`/`job_identity`/`trusted_redirects`'s own
design: `app.applications.browser_runtime` supplies the real DOM
observations (did the route change, did the field set change, is a
validation-error-shaped element present, what does the page text say), this
module only judges them. Deliberately conservative: a stalled advance is
only ever classified VALIDATION_BLOCKED when real evidence (a validation-
shaped DOM element or an explicit validation phrase) is present -- when
nothing changed and no such evidence exists either, the honest result is
NO_CHANGE_UNKNOWN, never guessed as either a block or a success."""

from dataclasses import dataclass
from enum import Enum

# Phrases a real inline validation error commonly uses. Deliberately texts
# that only make sense as a validation complaint (never a plain word like
# "required" alone, which could appear in unrelated page copy, e.g. a job
# description listing "required skills").
VALIDATION_ERROR_PHRASES = (
    "is required", "this field is required", "this is a required field",
    "please enter", "please fill", "please select", "please complete",
    "field is required", "cannot be blank", "must not be empty",
    "invalid entry", "please provide", "please correct the following",
    "please fix the errors below",
)


class AdvanceOutcome(str, Enum):
    ADVANCED = "ADVANCED"
    VALIDATION_BLOCKED = "VALIDATION_BLOCKED"
    NO_CHANGE_UNKNOWN = "NO_CHANGE_UNKNOWN"


def has_validation_error_text(body_text: str) -> bool:
    lowered = (body_text or "").strip().lower()
    return any(p in lowered for p in VALIDATION_ERROR_PHRASES)


@dataclass(frozen=True)
class AdvanceAttempt:
    """Real observations `browser_runtime` gathers about ONE Next/Continue
    click attempt. `route_changed`/`fields_changed` cover the two ways a
    genuine advance is normally detected already (URL change, or a new
    field-set fingerprint for a client-side-only step swap); this module is
    only ever consulted when BOTH are false, i.e. nothing about the page
    changed."""
    route_changed: bool
    fields_changed: bool
    validation_error_elements: int = 0
    body_text: str = ""


def classify_advance_attempt(attempt: AdvanceAttempt) -> AdvanceOutcome:
    if attempt.route_changed or attempt.fields_changed:
        return AdvanceOutcome.ADVANCED
    if attempt.validation_error_elements > 0 or has_validation_error_text(attempt.body_text):
        return AdvanceOutcome.VALIDATION_BLOCKED
    return AdvanceOutcome.NO_CHANGE_UNKNOWN
