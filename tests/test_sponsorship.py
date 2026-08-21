from app.models import SponsorshipStatus
from app.sponsorship.classifier import classify_sponsorship


def test_no_sponsorship_hard_skip():
    status, evidence = classify_sponsorship(
        "We are unable to sponsor visas for this role, now or in the future.", "SomeCo"
    )
    assert status == SponsorshipStatus.NO_SPONSORSHIP
    assert "sponsor" in evidence.lower()


def test_confirmed_sponsor():
    status, _ = classify_sponsorship("Visa sponsorship available for qualified candidates.", "SomeCo")
    assert status == SponsorshipStatus.CONFIRMED_SPONSOR


def test_likely_sponsor_from_known_employer(tmp_env):
    status, evidence = classify_sponsorship("Join our backend team building APIs.", "Acme Corp")
    assert status == SponsorshipStatus.LIKELY_SPONSOR
    assert "review" in evidence.lower()


def test_unknown_when_no_evidence(tmp_env):
    status, _ = classify_sponsorship("Join our backend team building APIs.", "Totally Unknown LLC")
    assert status == SponsorshipStatus.UNKNOWN


def test_no_sponsorship_takes_priority_over_known_employer(tmp_env):
    status, _ = classify_sponsorship("We do not offer visa sponsorship for this role.", "Acme Corp")
    assert status == SponsorshipStatus.NO_SPONSORSHIP
