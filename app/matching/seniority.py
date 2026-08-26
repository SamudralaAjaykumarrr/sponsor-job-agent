import re

# Titles that typically sit well above the candidate's ~3 YOE level. A senior
# title alone doesn't hard-skip -- it only hard-skips when JD years-required
# evidence isn't compatible with ~3 years, per CLAUDE.md: "Senior titles
# should only pass if the actual JD requirements are compatible."
SENIOR_TITLE_TOKENS = [
    "staff", "principal", "architect", "director", "vp", "vice president",
]

_YEARS_PATTERN = re.compile(
    # Real bug caught live during pumpcareers canary prep: a range separator
    # of only ASCII "-" missed en-dash (–) / em-dash (—), which
    # rich-text JD editors (Greenhouse's included) commonly emit for "N-M
    # years" ranges. Without matching them, "3–15 years" isn't
    # recognized as a range at all -- the regex skips past the unmatched
    # dash and the unconsumed lower bound, then matches standalone "15
    # years", extracting the RANGE'S UPPER bound as if it were the sole/
    # minimum requirement (3–15 years actually means a floor of 3).
    r"(\d{1,2})\s*\+?\s*(?:[-–—]|to)?\s*(\d{1,2})?\s*\+?\s*years?", re.IGNORECASE
)

MAX_ACCEPTABLE_YEARS = 5
HARD_SKIP_YEARS = 7


def extract_min_years_required(text: str) -> int | None:
    """Best-effort extraction of the first 'N years' / 'N-M years' / 'N+ years'
    figure mentioned in the JD. Returns the lower bound of the first match, or
    None if no such figure appears. This is a heuristic, not a guarantee."""
    if not text:
        return None
    m = _YEARS_PATTERN.search(text)
    if not m:
        return None
    return int(m.group(1))


def evaluate_seniority(title: str, description: str) -> tuple[bool, str, int | None]:
    """Returns (passes, reason, required_years).
    Hard-skips: JD explicitly requires 7+ years, or the title is senior-tier
    (Staff/Principal/Architect/Director/VP) and the JD's stated years
    requirement (if any) exceeds what's compatible with ~3 YOE."""
    title_lower = (title or "").lower()
    required_years = extract_min_years_required(description or "")

    if required_years is not None and required_years >= HARD_SKIP_YEARS:
        return False, f"JD requires {required_years}+ years of experience (candidate has ~3).", required_years

    is_senior_title = any(re.search(rf"\b{re.escape(tok)}\b", title_lower) for tok in SENIOR_TITLE_TOKENS)
    if is_senior_title:
        if required_years is not None and required_years <= MAX_ACCEPTABLE_YEARS:
            return True, f"Senior-tier title but JD states {required_years}+ years, compatible with candidate.", required_years
        return (
            False,
            f"Senior-tier title ('{title}') without JD evidence of a requirement "
            f"compatible with ~3 years of experience.",
            required_years,
        )

    return True, "Title/seniority compatible with candidate experience.", required_years
