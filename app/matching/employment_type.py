NON_FULL_TIME_SIGNALS = [
    "part-time", "part time", "internship", "intern position", "contract-to-hire",
    "contractor", "temporary position", "seasonal", "1099", "c2c", "corp-to-corp",
]


def is_full_time(employment_type_raw: str, description: str) -> bool:
    """Full-time-only per CLAUDE.md search rules. Conservative: only rejects on
    an EXPLICIT non-full-time signal; ambiguous/missing employment-type data is
    treated as full-time rather than rejected (consistent with the salary rule
    of never rejecting for missing information). Used by the discovery-time
    filter (app.agent.cycle) -- deliberately unchanged by Phase 8. For the
    application EXECUTOR's stricter positive-classification gate, see
    classify_employment_type() below."""
    combined = f"{employment_type_raw} {description}".lower()
    return not any(signal in combined for signal in NON_FULL_TIME_SIGNALS)


# --- Phase 8 (CLAUDE.md Phase 8 section 1): positive classification for the
# application executor's hard gate. Unlike is_full_time() above (permissive:
# "not explicitly non-full-time"), the executor must never auto-submit
# without an EXPLICIT FULL_TIME signal -- an ambiguous/silent job is UNKNOWN,
# never FULL_TIME, here.
from app.models import EmploymentType  # noqa: E402

_STRUCTURED_FULL_TIME_TOKENS = ("full_time", "full-time", "fulltime", "full time", "permanent")

# Ordered so a more specific/severe signal is checked before a weaker one
# that might also appear in the same text (e.g. "contract-to-hire" contains
# "contract").
_NEGATIVE_TYPE_SIGNALS: list[tuple[EmploymentType, tuple[str, ...]]] = [
    (EmploymentType.C2C, ("c2c", "corp-to-corp", "corp to corp")),
    (EmploymentType.INTERNSHIP, ("internship", "intern position", "co-op", "coop position")),
    (EmploymentType.TEMPORARY, ("temporary position", "temp position", "temporary role")),
    (EmploymentType.SEASONAL, ("seasonal",)),
    (EmploymentType.FREELANCE, ("freelance", "1099")),
    (EmploymentType.PART_TIME, ("part-time", "part time", "parttime")),
    (EmploymentType.CONTRACT, (
        "contract-to-hire", "contract to hire", "contractor", "w2 contract",
        "contract position", "contract role", "this is a contract", "contract",
    )),
]


def classify_employment_type(employment_type_raw: str, title: str = "", description: str = "") -> EmploymentType:
    """Positive employment-type classification for the Phase 8 executor gate.

    Order of evidence: an explicit structured `employment_type_raw` value is
    trusted first (a provider that reports one is the strongest signal we
    have and is never fabricated -- see app/providers/*). Only when that is
    empty/ambiguous do we fall back to scanning title+description text, and
    only for an EXPLICIT signal either way. Silence in every source is
    UNKNOWN, never FULL_TIME -- "if employment type is unknown: do not
    auto-submit" (CLAUDE.md Phase 8 section 1)."""
    raw_lower = (employment_type_raw or "").strip().lower()

    if raw_lower:
        for etype, tokens in _NEGATIVE_TYPE_SIGNALS:
            if any(tok in raw_lower for tok in tokens):
                return etype
        if any(tok in raw_lower for tok in _STRUCTURED_FULL_TIME_TOKENS):
            return EmploymentType.FULL_TIME

    combined = f"{title or ''} {description or ''}".lower()
    for etype, tokens in _NEGATIVE_TYPE_SIGNALS:
        if any(tok in combined for tok in tokens):
            return etype

    if any(tok in combined for tok in _STRUCTURED_FULL_TIME_TOKENS):
        return EmploymentType.FULL_TIME

    return EmploymentType.UNKNOWN
