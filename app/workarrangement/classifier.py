import re

from app.models import WorkArrangement

REMOTE_PATTERNS = [
    r"\bfully\s+remote\b",
    r"\b100%\s+remote\b",
    r"\bremote[\s-]?first\b",
    r"\bwork\s+from\s+home\b",
    r"\bwfh\b",
    r"\bremote\b",
]

HYBRID_PATTERNS = [
    r"\bhybrid\b",
]

# "N days a week in office" only signals HYBRID when N is a PARTIAL week
# (1-4) -- a real JD's "5 days a week in office" (a standard, full onsite
# week, paired elsewhere with "No remote work will be considered") matched
# the old un-bounded \d+ pattern and was mis-tagged HYBRID, silently
# outranking ONSITE in the priority tier despite the JD's own explicit
# no-remote statement. Caught live during pumpcareers canary prep.
_DAYS_IN_OFFICE_PATTERN = re.compile(
    r"([1-4])\s*days?\s*(?:a|per)\s*week\s+(?:in[\s-]?office|onsite|in\s+the\s+office)"
    r"|in[\s-]?office\s+([1-4])\s*days?",
    re.IGNORECASE,
)

ONSITE_PATTERNS = [
    r"\bon[\s-]?site\b",
    r"\bin[\s-]?person\b",
    r"\bno\s+remote\b",
    r"\bmust\s+work\s+from\s+(?:office|the\s+office)\b",
    r"\bon[\s-]?location\b",
]


def _matches_any(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def classify_work_arrangement(location: str, description: str) -> WorkArrangement:
    combined = f"{location}\n{description}".lower()

    is_remote = _matches_any(REMOTE_PATTERNS, combined)
    is_hybrid = _matches_any(HYBRID_PATTERNS, combined) or _DAYS_IN_OFFICE_PATTERN.search(combined) is not None
    is_onsite = _matches_any(ONSITE_PATTERNS, combined)

    # Hybrid/onsite signals win over a bare "remote" mention elsewhere in the text
    # (e.g. "no remote work available" also contains the word "remote").
    if is_hybrid:
        return WorkArrangement.HYBRID
    if is_onsite:
        return WorkArrangement.ONSITE
    if is_remote:
        return WorkArrangement.REMOTE

    if "remote" in (location or "").lower():
        return WorkArrangement.REMOTE

    return WorkArrangement.UNKNOWN
