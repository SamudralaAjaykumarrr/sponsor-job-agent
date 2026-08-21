from datetime import datetime, timedelta, timezone

from app.freshness.tracker import compute_freshness
from app.models import FreshnessTier


def test_maximum_freshness():
    now = datetime.now(timezone.utc)
    published = (now - timedelta(minutes=30)).isoformat()
    assert compute_freshness(published, published, now=now) == FreshnessTier.MAXIMUM


def test_very_high_freshness():
    now = datetime.now(timezone.utc)
    published = (now - timedelta(hours=2)).isoformat()
    assert compute_freshness(published, published, now=now) == FreshnessTier.VERY_HIGH


def test_high_freshness():
    now = datetime.now(timezone.utc)
    published = (now - timedelta(hours=6)).isoformat()
    assert compute_freshness(published, published, now=now) == FreshnessTier.HIGH


def test_moderate_freshness():
    now = datetime.now(timezone.utc)
    published = (now - timedelta(hours=20)).isoformat()
    assert compute_freshness(published, published, now=now) == FreshnessTier.MODERATE


def test_lower_freshness_old():
    now = datetime.now(timezone.utc)
    published = (now - timedelta(days=5)).isoformat()
    assert compute_freshness(published, published, now=now) == FreshnessTier.LOWER


def test_falls_back_to_first_seen_when_no_published():
    now = datetime.now(timezone.utc)
    first_seen = (now - timedelta(minutes=10)).isoformat()
    assert compute_freshness(None, first_seen, now=now) == FreshnessTier.MAXIMUM


def test_unparseable_defaults_to_lower():
    assert compute_freshness("not-a-date", "also-not-a-date") == FreshnessTier.LOWER
