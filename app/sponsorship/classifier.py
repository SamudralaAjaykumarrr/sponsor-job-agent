import json
import re

from app.config import KNOWN_SPONSORS_PATH
from app.models import SponsorshipStatus

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
    r"not\s+sponsoring",
    r"no\s+c2c",
    r"unable\s+to\s+provide\s+(?:visa\s+)?sponsorship",
    r"not\s+authorized\s+to\s+sponsor",
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


def _load_known_sponsors() -> list[str]:
    if not KNOWN_SPONSORS_PATH.exists():
        return []
    data = json.loads(KNOWN_SPONSORS_PATH.read_text())
    return [e.lower() for e in data.get("employers", [])]


def _find_match(patterns: list[str], text: str) -> str | None:
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(0)
    return None


def classify_sponsorship(description: str, company: str) -> tuple[SponsorshipStatus, str]:
    """Returns (status, evidence_text). NO_SPONSORSHIP is checked first (hard skip),
    then CONFIRMED_SPONSOR, then LIKELY_SPONSOR via the local known-employer list,
    else UNKNOWN. Historical sponsorship alone is never treated as proof for a
    specific role -- it only ever yields LIKELY_SPONSOR (review-only)."""
    text = description or ""

    no_match = _find_match(NO_SPONSORSHIP_PATTERNS, text)
    if no_match:
        return SponsorshipStatus.NO_SPONSORSHIP, f"JD states: \"{no_match}\""

    confirmed_match = _find_match(CONFIRMED_SPONSOR_PATTERNS, text)
    if confirmed_match:
        return SponsorshipStatus.CONFIRMED_SPONSOR, f"JD states: \"{confirmed_match}\""

    known_sponsors = _load_known_sponsors()
    company_lower = (company or "").strip().lower()
    for sponsor in known_sponsors:
        if sponsor and (sponsor == company_lower or sponsor in company_lower):
            return (
                SponsorshipStatus.LIKELY_SPONSOR,
                f"Employer '{company}' has recent H-1B filing history (local reference list); "
                "this specific role does not explicitly confirm sponsorship -- review manually.",
            )

    return SponsorshipStatus.UNKNOWN, "No explicit sponsorship statement and no employer history match."
