from app.models import ApplicationMode, Job
from app.pipeline import ingest_and_process
from app.sponsorship.review_queue import build_review_queue


def _mk_job(company, description, title="Backend Software Engineer"):
    job = Job(title=title, company=company, location="Remote", description=description, mode=ApplicationMode.ASSIST)
    return ingest_and_process(job)


def test_review_queue_only_contains_likely_sponsor(tmp_env):
    _mk_job("Acme Corp", "Join our backend team building APIs in Python.")  # LIKELY via known list
    _mk_job("BrandNewCo", "H-1B sponsorship available for this role.")  # CONFIRMED
    _mk_job("NoSponsorCo", "We are unable to sponsor visas now or in the future.")  # NO_SPONSORSHIP

    queue = build_review_queue()
    assert len(queue) == 1
    assert queue[0].company == "Acme Corp"
    assert queue[0].missing_confirmation


def test_review_queue_has_explanation_and_history_field(tmp_env):
    _mk_job("Globex", "Join our backend team building scalable Python APIs.")
    queue = build_review_queue()
    assert len(queue) == 1
    item = queue[0]
    assert item.historical_strength in ("NONE", "SOME", "OLD", "STRONG_RECENT")
    assert isinstance(item.reasons, list)
