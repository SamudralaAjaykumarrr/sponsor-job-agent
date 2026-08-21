NON_FULL_TIME_SIGNALS = [
    "part-time", "part time", "internship", "intern position", "contract-to-hire",
    "contractor", "temporary position", "seasonal", "1099", "c2c", "corp-to-corp",
]


def is_full_time(employment_type_raw: str, description: str) -> bool:
    """Full-time-only per CLAUDE.md search rules. Conservative: only rejects on
    an EXPLICIT non-full-time signal; ambiguous/missing employment-type data is
    treated as full-time rather than rejected (consistent with the salary rule
    of never rejecting for missing information)."""
    combined = f"{employment_type_raw} {description}".lower()
    return not any(signal in combined for signal in NON_FULL_TIME_SIGNALS)
