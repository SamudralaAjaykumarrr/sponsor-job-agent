import re

PRIMARY_ROLES = [
    "software engineer",
    "software engineer ii",
    "backend engineer",
    "backend software engineer",
    "python engineer",
    "python developer",
    "api engineer",
    "platform engineer",
    "cloud software engineer",
    "application engineer",
]

SECONDARY_ROLES = [
    "devops engineer",
    "cloud engineer",
    "infrastructure engineer",
    "sdet",
    "qa automation engineer",
    "systems engineer",
    "data platform engineer",
]

ALL_TARGET_ROLES = PRIMARY_ROLES + SECONDARY_ROLES

# Broad tokens that indicate a CS/STEM technical role even if the exact title
# isn't in the target list -- used only to avoid hard-skipping close variants
# (e.g. "Senior Backend Engineer", "Full Stack Software Engineer").
STEM_SIGNAL_TOKENS = [
    "engineer",
    "developer",
    "software",
    "backend",
    "python",
    "api",
    "platform",
    "cloud",
    "devops",
    "infrastructure",
    "sdet",
    "qa automation",
    "systems engineer",
    "data",
]


def is_target_role(title: str) -> tuple[bool, bool]:
    """Returns (is_relevant, is_primary)."""
    t = (title or "").lower()
    for role in PRIMARY_ROLES:
        if role in t:
            return True, True
    for role in SECONDARY_ROLES:
        if role in t:
            return True, False
    # Fallback: loose STEM/engineering signal so near-miss titles aren't hard-skipped,
    # but they are treated as secondary (not primary) matches.
    if any(re.search(rf"\b{re.escape(tok)}\b", t) for tok in STEM_SIGNAL_TOKENS):
        return True, False
    return False, False
