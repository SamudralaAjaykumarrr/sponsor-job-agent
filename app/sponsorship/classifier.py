"""Current-role sponsorship pattern classifier -- the hard gate. Reads ONLY
the JD text + company name + the local known-sponsors reference list. Never
imports app.sponsorship.evidence/profile/decision (CLAUDE.md Phase 6 section
27, reaffirmed Phase 7): historical employer evidence is blended in one layer
up, by app.sponsorship.decision.decide_sponsorship(), which calls
classify_sponsorship_detailed() below and then may only ever upgrade an
UNKNOWN result to LIKELY_SPONSOR -- never touch NO_SPONSORSHIP or
CONFIRMED_SPONSOR, and never produce CONFIRMED_SPONSOR itself."""

import json
import re
from dataclasses import dataclass, field

from app.config import KNOWN_SPONSORS_PATH
from app.models import SponsorshipStatus

CLASSIFIER_VERSION = "phase7.1"

NO_SPONSORSHIP_PATTERNS = [
    r"no\s+(?:visa\s+)?sponsorship",
    r"not\s+(?:able|in\s+a\s+position)\s+to\s+sponsor",
    r"unable\s+to\s+sponsor",
    r"will\s+not\s+sponsor",
    r"cannot\s+sponsor",
    r"can(?:'|no)t\s+sponsor",
    r"without\s+sponsorship\s+now\s+or\s+in\s+the\s+future",
    r"must\s+not\s+require\s+sponsorship",
    r"do(?:es)?\s+not\s+(?:provide|offer)\s+(?:visa\s+)?sponsorship",
    r"does\s+not\s+support\s+sponsorship",
    r"not\s+sponsoring",
    r"no\s+c2c",
    r"unable\s+to\s+provide\s+(?:visa\s+)?sponsorship",
    r"not\s+authorized\s+to\s+sponsor",
    r"sponsorship\s+will\s+not\s+be\s+(?:provided|available|considered)",
    r"requiring\s+sponsorship\s+will\s+not\s+be\s+considered",
    r"require\s+sponsorship\s+will\s+not\s+be\s+considered",
    r"u\.?\s?s\.?\s+citizens?\s+only",
    r"must\s+be\s+authorized\s+to\s+work\s+(?:in\s+the\s+u\.?s\.?\s+)?without\s+(?:the\s+need\s+for\s+)?sponsorship",
    r"permanent\s+work\s+authorization\s+required",
    r"authorized\s+to\s+work\s+without\s+sponsorship",
]

CONFIRMED_SPONSOR_PATTERNS = [
    r"sponsorship\s+(?:is\s+)?available",
    r"will\s+sponsor",
    r"we\s+sponsor",
    r"h-?1b\s+sponsorship\s+(?:provided|available|offered)",
    r"visa\s+sponsorship\s+available",
    r"open\s+to\s+(?:visa\s+)?sponsorship",
    r"sponsors?\s+work\s+visas",
    r"sponsorship\s+(?:is\s+)?offered",
    r"provides?\s+visa\s+sponsorship",
    r"h-?1b\s+transfer(?:s)?\s+welcome",
]

# CLAUDE.md Phase 7 section 18: conditional/case-by-case language is never an
# explicit confirmation by itself.
CONDITIONAL_SPONSOR_PATTERNS = [
    r"may\s+sponsor",
    r"case-by-case",
    r"case\s+by\s+case",
    r"depending\s+on\s+(?:the\s+)?candidate",
    r"limited\s+sponsorship",
    r"certain\s+visa\s+types?\s+only",
    r"sponsorship\s+may\s+be\s+(?:considered|available)",
    r"sponsor\s+exceptional\s+candidates",
    r"willing\s+to\s+sponsor\s+(?:for\s+)?(?:the\s+)?right\s+candidate",
]


@dataclass
class ClassificationResult:
    status: SponsorshipStatus
    evidence_text: str
    reasons: list[str] = field(default_factory=list)
    positive_matches: list[str] = field(default_factory=list)
    negative_matches: list[str] = field(default_factory=list)
    conditional_matches: list[str] = field(default_factory=list)
    conflict: bool = False
    conditional: bool = False
    blocking_reason: str = ""


def _load_known_sponsors() -> list[str]:
    if not KNOWN_SPONSORS_PATH.exists():
        return []
    data = json.loads(KNOWN_SPONSORS_PATH.read_text())
    return [e.lower() for e in data.get("employers", [])]


def _find_all_matches(patterns: list[str], text: str) -> list[str]:
    matches = []
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            matches.append(m.group(0))
    return matches


def classify_sponsorship_detailed(description: str, company: str) -> ClassificationResult:
    """Full current-role classification with evidence spans + conflict/
    conditional detection (CLAUDE.md Phase 7 sections 16-20). Deterministic
    rule order:
      1. Both positive AND negative language present in the same JD ->
         CONFLICT: LIKELY_SPONSOR (review-only), never hard-skip, never
         CONFIRMED -- a human must resolve it.
      2. Negative language alone -> NO_SPONSORSHIP (hard skip). Dominant over
         conditional language too, since safety comes first.
      3. Positive language alone -> CONFIRMED_SPONSOR.
      4. Conditional/case-by-case language alone -> LIKELY_SPONSOR, flagged
         conditional (never CONFIRMED per section 18).
      5. Otherwise falls back to the local known-sponsors reference list ->
         LIKELY_SPONSOR (review-only), else UNKNOWN.
    """
    text = description or ""

    negative_matches = _find_all_matches(NO_SPONSORSHIP_PATTERNS, text)
    positive_matches = _find_all_matches(CONFIRMED_SPONSOR_PATTERNS, text)
    conditional_matches = _find_all_matches(CONDITIONAL_SPONSOR_PATTERNS, text)

    if negative_matches and positive_matches:
        return ClassificationResult(
            status=SponsorshipStatus.LIKELY_SPONSOR,
            evidence_text=f"Conflicting JD language: positive \"{positive_matches[0]}\" AND "
                          f"negative \"{negative_matches[0]}\" both present -- review required.",
            reasons=[
                f"positive sponsorship language found: \"{positive_matches[0]}\"",
                f"negative sponsorship language also found: \"{negative_matches[0]}\"",
                "current role contains conflicting sponsorship signals",
            ],
            positive_matches=positive_matches, negative_matches=negative_matches,
            conditional_matches=conditional_matches, conflict=True,
            blocking_reason="current role JD contains both positive and negative sponsorship language -- "
                             "do not auto-apply, resolve manually",
        )

    if negative_matches:
        return ClassificationResult(
            status=SponsorshipStatus.NO_SPONSORSHIP,
            evidence_text=f"JD states: \"{negative_matches[0]}\"",
            reasons=[f"JD states: \"{negative_matches[0]}\""],
            negative_matches=negative_matches,
            blocking_reason="current role explicitly states no sponsorship",
        )

    if positive_matches:
        return ClassificationResult(
            status=SponsorshipStatus.CONFIRMED_SPONSOR,
            evidence_text=f"JD states: \"{positive_matches[0]}\"",
            reasons=[f"JD states: \"{positive_matches[0]}\"", "current role explicitly confirms sponsorship"],
            positive_matches=positive_matches,
        )

    if conditional_matches:
        return ClassificationResult(
            status=SponsorshipStatus.LIKELY_SPONSOR,
            evidence_text=f"JD contains conditional sponsorship language: \"{conditional_matches[0]}\"",
            reasons=[
                f"JD contains conditional/case-by-case sponsorship language: \"{conditional_matches[0]}\"",
                "not an explicit confirmation -- review required",
            ],
            conditional_matches=conditional_matches, conditional=True,
            blocking_reason="current role lacks explicit (unconditional) sponsorship confirmation",
        )

    known_sponsors = _load_known_sponsors()
    company_lower = (company or "").strip().lower()
    for sponsor in known_sponsors:
        if sponsor and (sponsor == company_lower or sponsor in company_lower):
            return ClassificationResult(
                status=SponsorshipStatus.LIKELY_SPONSOR,
                evidence_text=f"Employer '{company}' has recent H-1B filing history (local reference list); "
                              "this specific role does not explicitly confirm sponsorship -- review manually.",
                reasons=[
                    f"employer '{company}' matches the local known-sponsors reference list",
                    "current JD does not explicitly confirm sponsorship",
                ],
                blocking_reason="current role lacks explicit sponsorship confirmation",
            )

    return ClassificationResult(
        status=SponsorshipStatus.UNKNOWN,
        evidence_text="No explicit sponsorship statement and no employer history match.",
        reasons=["no explicit current-role sponsorship statement", "no known-employer reference match"],
        blocking_reason="insufficient current-role evidence",
    )


def classify_sponsorship(description: str, company: str) -> tuple[SponsorshipStatus, str]:
    """Backward-compatible entry point (Phase 1-6 signature, unchanged
    behavior for every pre-Phase-7 case) -- returns (status, evidence_text).
    Callers that want the full explanation/conflict detail should use
    classify_sponsorship_detailed() or app.sponsorship.decision.decide_sponsorship()
    instead."""
    result = classify_sponsorship_detailed(description, company)
    return result.status, result.evidence_text
