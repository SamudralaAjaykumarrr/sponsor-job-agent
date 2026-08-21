from datetime import datetime, timezone

from app.models import FreshnessTier


def _parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def compute_freshness(published_at: str | None, first_seen_at: str, now: datetime | None = None) -> FreshnessTier:
    """Prefer published_at when reliable/parseable, else fall back to first_seen_at."""
    now = now or datetime.now(timezone.utc)

    reference = _parse_dt(published_at) if published_at else None
    if reference is None:
        reference = _parse_dt(first_seen_at)
    if reference is None:
        return FreshnessTier.LOWER

    age_minutes = (now - reference).total_seconds() / 60.0

    if age_minutes < 0:
        age_minutes = 0

    if age_minutes <= 60:
        return FreshnessTier.MAXIMUM
    if age_minutes <= 180:
        return FreshnessTier.VERY_HIGH
    if age_minutes <= 720:
        return FreshnessTier.HIGH
    if age_minutes <= 1440:
        return FreshnessTier.MODERATE
    return FreshnessTier.LOWER
