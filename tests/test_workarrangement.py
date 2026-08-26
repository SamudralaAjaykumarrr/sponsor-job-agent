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


def test_five_days_a_week_in_office_is_onsite_not_hybrid():
    # Real bug caught live during pumpcareers canary prep: "5 days a week
    # in office" (a standard full onsite week, no remote component at all)
    # matched the old un-bounded "N days a week in office" hybrid pattern
    # and was mis-tagged HYBRID, outranking ONSITE despite an explicit
    # "No remote work" statement in the same JD.
    assert classify_work_arrangement(
        "San Francisco, CA", "We are 5 days a week in office. No remote work will be considered.",
    ) == WorkArrangement.ONSITE


def test_partial_days_a_week_in_office_stays_hybrid():
    assert classify_work_arrangement("Austin, TX", "In office 2 days a week.") == WorkArrangement.HYBRID
