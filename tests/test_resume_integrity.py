"""CLAUDE.md Phase 13 sections 43-45: resume/JD fingerprint linkage. Pure
function over a Job model -- never touches network/browser/DB."""

from app.applications.resume_integrity import verify_resume_freshness
from app.models import Job


def _job(**overrides) -> Job:
    base = dict(title="Software Engineer", company="Acme", description="JD text")
    base.update(overrides)
    return Job(**base)


def test_no_resume_fingerprint_recorded_assumed_fresh():
    job = _job(resume_jd_fingerprint="", jd_sponsorship_fingerprint="abc")
    result = verify_resume_freshness(job)
    assert result.fresh is True


def test_no_current_jd_fingerprint_assumed_fresh():
    job = _job(resume_jd_fingerprint="abc", jd_sponsorship_fingerprint="")
    result = verify_resume_freshness(job)
    assert result.fresh is True


def test_matching_fingerprints_are_fresh():
    job = _job(resume_jd_fingerprint="abc123", jd_sponsorship_fingerprint="abc123")
    result = verify_resume_freshness(job)
    assert result.fresh is True


def test_diverged_fingerprints_are_stale():
    job = _job(resume_jd_fingerprint="abc123", jd_sponsorship_fingerprint="def456")
    result = verify_resume_freshness(job)
    assert result.fresh is False
    assert "regenerate" in result.reason.lower()
