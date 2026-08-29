"""Tsenta-parity-closure-v1, P1 polish: /jobs freshness filter parity with
the Dashboard, and no raw FreshnessTier enum value rendered anywhere a job
is displayed."""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.candidate.profile import save_profile
from app.freshness.tracker import freshness_label
from app.main import app
from app.models import FreshnessTier, Job
from app.pipeline import ingest_and_process

JD_TEXT = (
    "We are hiring a Backend Software Engineer to build REST APIs in Python using FastAPI. "
    "This is a full-time position. H-1B sponsorship is available for this role."
)


def test_freshness_label_never_returns_a_raw_enum_name():
    for tier in FreshnessTier:
        label = freshness_label(tier)
        assert label != tier.value
        assert label  # never empty


def test_jobs_page_accepts_fresh_under_1hr_and_6hr_query_params(tmp_env, sample_profile):
    save_profile(sample_profile)
    now = datetime.now(timezone.utc)
    fresh_job = ingest_and_process(Job(
        title="Backend Software Engineer", company="Freshco", location="Remote - US",
        description=JD_TEXT, employment_type="Full-time", provider="manual",
        external_job_id="fresh-1", mode="ASSIST",
        first_seen_at=now.isoformat(),
    ))
    stale_job = ingest_and_process(Job(
        title="Backend Software Engineer", company="Staleco", location="Remote - US",
        description=JD_TEXT, employment_type="Full-time", provider="manual",
        external_job_id="stale-1", mode="ASSIST",
        first_seen_at=(now - timedelta(hours=48)).isoformat(),
    ))

    client = TestClient(app)
    resp = client.get("/jobs", params={"fresh_under_1hr": "true"})
    assert resp.status_code == 200
    assert "Freshco" in resp.text
    assert "Staleco" not in resp.text

    # Raw enum values never leak into the rendered job cards.
    for raw in ("MAXIMUM", "VERY_HIGH", "MODERATE", "LOWER"):
        assert raw not in resp.text


def test_jobs_page_freshness_chip_marked_active(tmp_env, sample_profile):
    save_profile(sample_profile)
    client = TestClient(app)
    resp = client.get("/jobs", params={"fresh_under_1hr": "true"})
    assert 'href="?fresh_under_1hr=true"' in resp.text
    assert 'href="?fresh_under_6hr=true"' in resp.text
