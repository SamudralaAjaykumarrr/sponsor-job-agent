import re

from app.config import MIN_SALARY_USD

_SALARY_RANGE_PATTERN = re.compile(
    r"\$\s?(\d{2,3}k|\d{2,3}(?:,\d{3})?)\s*(?:-|to|–)\s*\$?\s?(\d{2,3}k|\d{2,3}(?:,\d{3})?)",
    re.IGNORECASE,
)


def _to_number(raw: str) -> float:
    raw = raw.strip().lower()
    if raw.endswith("k"):
        return float(raw[:-1]) * 1000
    return float(raw.replace(",", ""))


def extract_salary_from_text(text: str) -> tuple[float | None, float | None]:
    """Best-effort fallback extraction of a $min-$max salary range from free
    JD text, used only when a provider doesn't supply structured salary
    fields. Returns (None, None) if no range is found."""
    if not text:
        return None, None
    m = _SALARY_RANGE_PATTERN.search(text)
    if not m:
        return None, None
    lo = _to_number(m.group(1))
    hi = _to_number(m.group(2))
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def evaluate_compensation(salary_min: float | None, salary_max: float | None) -> tuple[bool, str]:
    """Reject ONLY when a clearly published maximum compensation is below the
    threshold. Never reject for unpublished salary (per CLAUDE.md)."""
    if salary_max is not None and salary_max < MIN_SALARY_USD:
        return False, f"Published max compensation ${salary_max:,.0f} is below ${MIN_SALARY_USD:,} threshold."
    if salary_min is not None or salary_max is not None:
        return True, f"Published salary (min=${salary_min}, max=${salary_max}) meets threshold."
    return True, "Salary not published -- not rejected on compensation grounds."
