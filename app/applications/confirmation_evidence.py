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
from typing import Optional

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
    heading_phrase_matched: bool = False,
    submit_control_disappeared: Optional[bool] = None,
    form_fields_disappeared: Optional[bool] = None,
) -> ConfirmationGrade:
    """CLAUDE.md Phase 13 sections 49-51, extended 2026-09-04 (multi-signal
    confirmation contract, after job 454/Anthropic's real canary returned
    UNRECOGNIZED_OUTCOME -- a body-text-only phrase list is too brittle
    against real-world wording variation):

      - `heading_phrase_matched`: the SAME curated phrase table, checked
        against the page's own accessible heading (<h1>/<h2>) instead of the
        whole body -- app.applications.confirmation_parser's separate,
        dedicated check. A heading is the primary semantic indicator of page
        state and is treated as an INDEPENDENT observation from the body
        scan, never a second vote for the identical text.
      - `submit_control_disappeared` / `form_fields_disappeared`: structural
        DOM signals (Optional -- None means "not observed/not applicable",
        contributing nothing, never treated as evidence of absence). ONLY
        when BOTH are explicitly True do they count as ONE corroborating
        signal, alongside a confirmation id or confirmation-shaped URL --
        never sufficient alone (a validation error or an unrelated page
        change could also make a form/button disappear), and requiring BOTH
        together is deliberately conservative.

      - STRONG: a trusted phrase matched (body OR heading) AND at least one
        of: a corroborating confirmation id, a confirmation-shaped URL,
        corroborating structural disappearance, OR the phrase was
        independently found in BOTH the body AND the heading (two
        independent locations agreeing is corroboration in its own right,
        equal in weight to an id/URL match).
      - MODERATE: a trusted phrase matched in exactly one of body/heading,
        with no other corroboration -- this project's existing curated
        phrase tables are already a strong single signal, so this alone is
        still sufficient to confirm, just not the strongest grade.
      - WEAK: only a confirmation id, a confirmation-shaped URL, or
        corroborating structural disappearance was observed WITHOUT any
        trusted phrase match (body or heading) -- never sufficient to
        confirm on its own.
      - NONE: nothing observed."""
    any_phrase_matched = phrase_matched or heading_phrase_matched
    both_locations_matched = phrase_matched and heading_phrase_matched
    structural_corroboration = bool(submit_control_disappeared) and bool(form_fields_disappeared)
    has_secondary = bool(confirmation_id) or url_looks_like_confirmation(current_url) or structural_corroboration
    if any_phrase_matched and (has_secondary or both_locations_matched):
        reason = "trusted success phrase matched with a corroborating confirmation id/URL/structural change" \
            if has_secondary else "trusted success phrase independently matched in both the body and the heading"
        return ConfirmationGrade(ConfirmationEvidenceStrength.STRONG, reason=reason)
    if any_phrase_matched:
        return ConfirmationGrade(
            ConfirmationEvidenceStrength.MODERATE, reason="trusted success phrase matched, no corroborating signal",
        )
    if has_secondary:
        return ConfirmationGrade(
            ConfirmationEvidenceStrength.WEAK,
            reason="only a confirmation id/URL shape/structural change observed, no trusted success phrase",
        )
    return ConfirmationGrade(ConfirmationEvidenceStrength.NONE, reason="no confirmation evidence observed")
