"""Employment-date recency helpers (JD intelligence v3). Used by
`app.resume_optimizer.optimizer` to weight experience-entry/bullet selection
and one-page overflow removal toward more recent roles (via
`app.resume_optimizer.relevance.score_experience_entry` and
`one_page.enforce_one_page`'s `entry_recency` parameter). Reads only
`app.candidate.schema.EmploymentEntry` dates -- never touches the JD.

Candidate profile dates are always "YYYY" or "YYYY-MM" strings, or the
literal "Present" for an ongoing role's end_date (see
app.candidate.schema.EmploymentEntry / every fixture in tests/conftest.py).
An unparsable/NEEDS_USER_INPUT date sorts as the oldest possible value
rather than raising -- recency is a ranking nicety, never a hard
dependency."""

import re

from app.candidate.schema import EmploymentEntry

_DATE_PATTERN = re.compile(r"(\d{4})(?:-(\d{1,2}))?")


def _parse_year_month(value: str) -> tuple[int, int]:
    if not value:
        return (0, 0)
    if value.strip().lower() == "present":
        return (9999, 12)
    m = _DATE_PATTERN.match(value.strip())
    if not m:
        return (0, 0)
    year = int(m.group(1))
    month = int(m.group(2)) if m.group(2) else 1
    return (year, month)


def _sort_key(entry: EmploymentEntry) -> tuple[tuple[int, int], tuple[int, int]]:
    return (_parse_year_month(entry.end_date), _parse_year_month(entry.start_date))


def recency_rank(employment: list[EmploymentEntry]) -> dict[str, float]:
    """Returns {company: 0..1}, where 1.0 is the most recent role (by
    end_date then start_date) and the score decreases evenly toward the
    oldest. Keyed by company name -- sufficiently unique within one
    candidate's own employment history for this ranking-signal purpose."""
    if not employment:
        return {}
    ordered = sorted(employment, key=_sort_key, reverse=True)
    n = len(ordered)
    if n == 1:
        return {ordered[0].company: 1.0}
    return {e.company: (n - 1 - i) / (n - 1) for i, e in enumerate(ordered)}
