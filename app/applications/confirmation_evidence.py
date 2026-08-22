"""Confirmation evidence STRENGTH grading (CLAUDE.md Phase 13 sections
49-51). Pure, dependency-free classification (no Playwright import) --
`app.applications.browser_runtime._do_capture_confirmation` supplies the
real observations (which trusted phrase matched, if any; whether a
confirmation id was extracted; whether the current URL itself looks like a
confirmation/thank-you route), this module only grades them.

The underlying phrase table (`browser_runtime._SUCCESS_PHRASES`) is already
curated to reject exactly the false positives CLAUDE.md section 50 lists
("Submit successfully to continue", "Your application will be received
after...", "Success stories", "Application confirmation will be emailed") --
none of those contain any of `_SUCCESS_PHRASES`' specific, affirmative,
completed-action phrasing. This module's job is grading the STRENGTH of a
genuine phrase match, never loosening what counts as a match at all."""

from dataclasses import dataclass
from enum import Enum

_URL_CONFIRMATION_HINTS = ("thank", "confirm", "success", "received", "complete")


class ConfirmationEvidenceStrength(str, Enum):
    """CLAUDE.md Phase 13 section 51's exact vocabulary."""
    STRONG = "STRONG"
    MODERATE = "MODERATE"
    WEAK = "WEAK"
    NONE = "NONE"


@dataclass(frozen=True)
class ConfirmationGrade:
    strength: ConfirmationEvidenceStrength
    reason: str = ""

    def confirms(self) -> bool:
        """CLAUDE.md Phase 13 section 51: only STRONG or MODERATE evidence
        may ever set an execution APPLIED -- WEAK/NONE must route to
        NEEDS_REVIEW / stay unconfirmed instead."""
        return self.strength in (ConfirmationEvidenceStrength.STRONG, ConfirmationEvidenceStrength.MODERATE)


def url_looks_like_confirmation(url: str) -> bool:
    lowered = (url or "").lower()
    return any(hint in lowered for hint in _URL_CONFIRMATION_HINTS)


def classify_confirmation_evidence(
    *, phrase_matched: bool, confirmation_id: str = "", current_url: str = "",
) -> ConfirmationGrade:
    """CLAUDE.md Phase 13 sections 49-51:
      - STRONG: a trusted success phrase matched AND (a confirmation id was
        extracted OR the URL itself looks like a confirmation route) --
        two independent corroborating signals.
      - MODERATE: a trusted success phrase matched alone -- this project's
        existing curated `_SUCCESS_PHRASES` table is already a strong single
        signal (deliberately specific, completed-action phrasing), so this
        alone is still sufficient to confirm, just not the strongest grade.
      - WEAK: only a confirmation id or a confirmation-shaped URL was
        observed WITHOUT a trusted phrase match -- modeled for completeness
        (a future provider-specific pattern might reach this path) but never
        reachable from the current phrase-gated capture flow, and never
        sufficient to confirm on its own.
      - NONE: nothing observed."""
    has_secondary = bool(confirmation_id) or url_looks_like_confirmation(current_url)
    if phrase_matched and has_secondary:
        return ConfirmationGrade(
            ConfirmationEvidenceStrength.STRONG,
            reason="trusted success phrase matched with a corroborating confirmation id/URL",
        )
    if phrase_matched:
        return ConfirmationGrade(
            ConfirmationEvidenceStrength.MODERATE, reason="trusted success phrase matched, no corroborating signal",
        )
    if has_secondary:
        return ConfirmationGrade(
            ConfirmationEvidenceStrength.WEAK,
            reason="only a confirmation id/URL shape observed, no trusted success phrase",
        )
    return ConfirmationGrade(ConfirmationEvidenceStrength.NONE, reason="no confirmation evidence observed")
