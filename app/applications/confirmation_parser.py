"""Provider-neutral confirmation/duplicate-application TEXT parsing (Real
Provider Execution V1).

This module is the SINGLE SOURCE of the success-phrase, duplicate-application
and confirmation-id tables that decide whether a real ATS page is showing a
genuine "your application was received" outcome. It is deliberately pure and
dependency-free (no Playwright import, no database access), exactly mirroring
how `app.applications.apply_entry` is the single source of apply-entry/
final-submit classification and `app.applications.confirmation_evidence` is
the single source of evidence GRADING -- three separate, non-overlapping
concerns:

  - `confirmation_parser` (this module): what does this page's TEXT say?
  - `confirmation_evidence`:              how STRONG is that observation?
  - `browser_runtime`:                    supplies the real observation.

These tables previously lived as private constants inside
`app.applications.browser_runtime`, which meant they could only be exercised
through a live Chromium session. `browser_runtime` now imports them from here
(never maintaining a second, parallel table -- the same rule CLAUDE.md's
Phase 11 section already fixed for apply-entry phrases), so the exact
production phrase tables can be tested directly against a local fixture's
HTML text, and a future provider adapter can reuse the identical parser
without touching the browser layer.

Rules this module keeps obeying:
  - `SUCCESS_PHRASES` are all deliberately affirmative, COMPLETED-action
    phrases ("thank you for applying", "application received"). A bare noun
    like "confirmation" is never a match, so text such as "Submit your
    application to receive confirmation by email" correctly does NOT confirm
    (CLAUDE.md Phase 11 section 35 / Phase 13 section 50).
  - `DUPLICATE_APPLICATION_PHRASES` are checked BEFORE any success phrase and
    are reported through a DISTINCT result field -- "you already applied" is
    evidence of a PRIOR application, never a fresh confirmation, and must
    never be folded into a CONFIRMED/APPLIED transition (CLAUDE.md Phase 11
    section 36).
  - The two phrase tables must stay MUTUALLY DISJOINT, so classification is
    always one unambiguous lookup rather than a priority tie-break (the same
    invariant CLAUDE.md's Phase 11 section imposes on
    NAVIGATION_SAFE_PHRASES/FINAL_SUBMIT_PHRASES/LOGIN_TRIGGER_PHRASES).
    `app.applications.doctor._check_confirmation_phrase_tables_disjoint`
    statically enforces this.
  - Parsing text is never, by itself, a decision. A caller must still route
    the result through `app.applications.confirmation_evidence
    .classify_confirmation_evidence()` and honour `ConfirmationGrade
    .confirms()` before anything may be marked APPLIED.
"""

import hashlib
import re
from dataclasses import dataclass

# Affirmative, completed-action phrases only -- see module docstring.
SUCCESS_PHRASES: tuple[str, ...] = (
    "thank you for applying", "application received", "application submitted", "successfully applied",
    "we've received your application", "we have received your application",
    "your application has been submitted", "thank you for your application", "thank you -- your application",
    # Real bug caught live (2026-08-31, Greenhouse Verified Submission
    # Contract V1's first real canary run, job 200/Robinhood): Greenhouse's
    # own default confirmation template phrases the body as "Thank you for
    # your interest in joining our [team description]!" rather than any of
    # the phrases above, while a page's <title> (never scanned -- only
    # `<body>` inner text is) separately says "Thank you for applying". This
    # phrase is deliberately company-name-free (never "...joining Robinhood",
    # which would only ever match one employer) so it generalizes to any
    # Greenhouse-hosted employer using this same default template.
    "thank you for your interest in joining",
)

# CLAUDE.md Phase 11 section 36: evidence of a PRIOR application.
DUPLICATE_APPLICATION_PHRASES: tuple[str, ...] = (
    "you have already applied", "already applied to this position", "already applied for this job",
    "you already applied", "application already submitted", "already submitted an application",
)

CONFIRMATION_ID_RE = re.compile(
    r"(?:confirmation|reference|application)\s*(?:number|id|#)?\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-]{3,})",
    re.I,
)

# How many characters of the page's own text are hashed into the durable
# `confirmation_text_fingerprint`. Bounded on purpose: a fingerprint is
# evidence that a specific confirmation message was observed, never a place
# to stash a page dump.
_FINGERPRINT_SNIPPET_CHARS = 300


@dataclass(frozen=True)
class ConfirmationParse:
    """One page's TEXT observations. Deliberately carries no verdict of its
    own -- `already_applied` and `phrase_matched` are independent facts, and
    grading them is `app.applications.confirmation_evidence`'s job."""
    phrase_matched: bool = False
    matched_phrase: str = ""
    already_applied: bool = False
    matched_duplicate_phrase: str = ""
    confirmation_id: str = ""
    text_fingerprint: str = ""

    def as_dict(self) -> dict:
        return {
            "phrase_matched": self.phrase_matched, "matched_phrase": self.matched_phrase,
            "already_applied": self.already_applied,
            "matched_duplicate_phrase": self.matched_duplicate_phrase,
            "confirmation_id": self.confirmation_id, "text_fingerprint": self.text_fingerprint,
        }


def find_success_phrase(text: str) -> str:
    """Returns the first trusted success phrase present in `text`, or "" --
    never a partial/fuzzy match, only a literal substring of the curated
    table."""
    lowered = (text or "").lower()
    return next((p for p in SUCCESS_PHRASES if p in lowered), "")


def find_duplicate_application_phrase(text: str) -> str:
    lowered = (text or "").lower()
    return next((p for p in DUPLICATE_APPLICATION_PHRASES if p in lowered), "")


def _looks_like_an_identifier(token: str) -> bool:
    """A confirmation id must contain at least one DIGIT.

    Without this, the keyword-anchored regex above happily captures the
    ordinary English word that follows the keyword: "Application received"
    yields "received", "Application submitted" yields "submitted". A real
    Lever-shaped confirmation fixture reproduced exactly that (caught by
    tests/test_confirmation_parser.py), and the consequence was not
    cosmetic -- a meaningless word would have been stored as a durable
    `confirmation_id` on the execution and its receipt, AND would have
    corroborated the evidence grade up from MODERATE to STRONG
    (`app.applications.confirmation_evidence` treats any non-empty
    confirmation id as a second independent signal). Inventing a second
    signal out of an English word is exactly the kind of inflated evidence
    this project forbids.

    Every genuine confirmation/reference id shape this project has actually
    observed contains digits ("ABC-1234-XYZ", "GH-2026-88134", "R-12345"),
    so requiring one is a pure precision improvement, never a loosened
    check: text with no identifier at all correctly yields "" rather than a
    fabricated one."""
    return any(ch.isdigit() for ch in token)


def extract_confirmation_id(text: str) -> str:
    """Returns the first keyword-anchored token that genuinely looks like an
    identifier, scanning ALL matches rather than only the first -- so a page
    whose first keyword occurrence is a false positive ("Application
    received ... Reference: XY-2211") still finds the real id."""
    for match in CONFIRMATION_ID_RE.finditer(text or ""):
        token = match.group(1)
        if _looks_like_an_identifier(token):
            return token
    return ""


def fingerprint_text(text: str) -> str:
    snippet = (text or "").strip()[:_FINGERPRINT_SNIPPET_CHARS]
    return hashlib.sha256(snippet.encode("utf-8")).hexdigest()[:24]


def parse_confirmation_text(text: str) -> ConfirmationParse:
    """The whole text-side observation in one call. A duplicate-application
    page short-circuits: it is reported with `already_applied=True` and
    `phrase_matched=False` so no caller can accidentally treat it as a fresh
    confirmation, even if the same page ALSO happens to contain a success
    phrase (a real "you already applied -- your application was received on
    ..." page does)."""
    duplicate_phrase = find_duplicate_application_phrase(text)
    if duplicate_phrase:
        return ConfirmationParse(
            phrase_matched=False, already_applied=True, matched_duplicate_phrase=duplicate_phrase,
        )
    phrase = find_success_phrase(text)
    # `confirmation_id` is extracted regardless of whether a success phrase
    # matched: a confirmation-id-shaped token WITHOUT a trusted phrase is
    # exactly the WEAK-evidence case `confirmation_evidence` models (and
    # never confirms on its own), so the grader must still see it.
    return ConfirmationParse(
        phrase_matched=bool(phrase), matched_phrase=phrase,
        confirmation_id=extract_confirmation_id(text), text_fingerprint=fingerprint_text(text),
    )
