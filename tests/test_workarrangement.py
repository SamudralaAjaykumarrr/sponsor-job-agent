from app.models import WorkArrangement
from app.workarrangement.classifier import classify_work_arrangement


def test_remote():
    assert classify_work_arrangement("Remote (US)", "This is a fully remote position.") == WorkArrangement.REMOTE


def test_hybrid():
    assert classify_work_arrangement("Austin, TX", "This is a hybrid role, 3 days a week in office.") == WorkArrangement.HYBRID


def test_onsite():
    assert classify_work_arrangement("Austin, TX", "This position is onsite, no remote work available.") == WorkArrangement.ONSITE


def test_unknown_defaults():
    assert classify_work_arrangement("Austin, TX", "We build great software.") == WorkArrangement.UNKNOWN


def test_location_remote_fallback():
    assert classify_work_arrangement("Remote", "We build great software.") == WorkArrangement.REMOTE
