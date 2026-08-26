"""Focused regression tests for two real bugs caught live while preparing
the first controlled Greenhouse canary preflight for a real posting
(job with a resume promoted through the Phase 14 resume_optimizer's nested
output/<job_id>/optimized/<variant_id>/ layout):

1. `app.applications.browser_assist._verify_resume()` rejected a resume
   artifact whose immediate parent directory name wasn't literally the
   job id -- true for the legacy flat `output/<job_id>/resume.pdf` layout,
   false for the resume_optimizer's nested layout. Fixed to use the same
   `/<job_id>/` path-segment convention as
   `app.applications.executor._verify_resume_artifact` and
   `app.applications.doctor._check_wrong_resume_job_mapping`.

2. `app.resume_optimizer.promotion.promote_variant()` wrote the resume
   optimizer's own internal `jd_fingerprint` (a different hash algorithm,
   different truncation length) into `jobs.resume_jd_fingerprint` -- a
   column that `app.applications.resume_integrity.verify_resume_freshness()`
   and `app.applications.approval.is_current_valid()` both compare directly
   against `jobs.jd_sponsorship_fingerprint`. This made every
   resume-optimizer-promoted resume look permanently stale to both checks.
   Fixed to write `job.jd_sponsorship_fingerprint` instead, matching the
   documented Phase 13 contract and `app.pipeline.generate_assist_outputs`'s
   own existing behavior.

Both tests below are pure/local -- no Playwright, no real network, no real
employer contacted."""

import hashlib

from app.models import ApplicationMode, Job
from app.pipeline import ingest_and_process
from app.resume_optimizer.optimizer import optimize_resume
from app.resume_optimizer.promotion import promote_current_variant, promote_variant

JD_TEXT = (
    "We are hiring a Backend Software Engineer to build REST APIs in Python using FastAPI, "
    "with PostgreSQL, Docker, and CI/CD pipelines. This is a full-time position. "
    "H-1B sponsorship is available for this role."
)


def _mock_job(external_job_id: str) -> Job:
    return Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description=JD_TEXT, employment_type="Full-time", provider="mock_ats",
        external_job_id=external_job_id, mode=ApplicationMode.ASSIST,
    )


# --- Bug 1: browser_assist._verify_resume() nested-path rejection ----------

def test_verify_resume_accepts_nested_optimizer_variant_path(tmp_env, sample_profile, monkeypatch):
    from app.candidate.profile import save_profile
    from app.applications.browser_assist import _verify_resume

    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("verify-resume-nested-1"))
    assert job.sponsorship_status.value != "UNKNOWN"

    optimize_resume(job.id)
    assert promote_current_variant(job.id) is True

    from app.jobs_repo import get_job
    promoted = get_job(job.id)
    # The promoted path must genuinely be the nested resume_optimizer shape
    # for this regression test to mean anything.
    assert f"/{promoted.id}/optimized/" in promoted.resume_pdf_path.replace("\\", "/")

    ok, reason, digest = _verify_resume(promoted)
    assert ok, f"expected nested optimizer variant path to verify ok, got: {reason}"
    assert digest == hashlib.sha256(open(promoted.resume_pdf_path, "rb").read()).hexdigest()


def test_verify_resume_still_rejects_a_path_for_a_different_job(tmp_env, sample_profile, tmp_path):
    from app.candidate.profile import save_profile
    from app.applications.browser_assist import _verify_resume

    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("verify-resume-wrong-job-1"))

    # A resume artifact that plainly belongs to some OTHER job id must still
    # be rejected -- the fix must not have widened the check into a no-op.
    wrong_job_dir = tmp_path / "output" / "999999" / "optimized" / "somevariant"
    wrong_job_dir.mkdir(parents=True)
    wrong_path = wrong_job_dir / "resume.pdf"
    wrong_path.write_bytes(b"%PDF-1.4 fake")

    from app.jobs_repo import update_job, get_job
    update_job(job.id, resume_pdf_path=str(wrong_path))
    job = get_job(job.id)

    ok, reason, digest = _verify_resume(job)
    assert not ok
    assert "does not correspond to this job" in reason
    assert digest == ""


# --- Bug 2: promote_variant() writing the wrong fingerprint scheme --------

def test_promote_variant_writes_sponsorship_scoped_fingerprint(tmp_env, sample_profile):
    from app.candidate.profile import save_profile
    from app.applications.resume_integrity import verify_resume_freshness
    from app.jobs_repo import get_job

    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("promote-fingerprint-1"))
    assert job.jd_sponsorship_fingerprint  # precondition: sponsorship analysis produced one

    result = optimize_resume(job.id)
    assert result.created

    from app.resume_optimizer.repo import get_current_variant
    variant = get_current_variant(job.id)
    # Precondition: the resume_optimizer's own internal fingerprint genuinely
    # differs in shape from the sponsorship one (different algorithm/length)
    # -- if this ever becomes equal by construction the original bug would
    # have been invisible, so assert the two schemes are actually distinct.
    assert variant["jd_fingerprint"] != job.jd_sponsorship_fingerprint
    assert len(variant["jd_fingerprint"]) != len(job.jd_sponsorship_fingerprint)

    promote_variant(job.id, variant)
    promoted = get_job(job.id)

    assert promoted.resume_jd_fingerprint == promoted.jd_sponsorship_fingerprint
    assert promoted.resume_jd_fingerprint != variant["jd_fingerprint"]

    freshness = verify_resume_freshness(promoted)
    assert freshness.fresh, f"expected freshly promoted resume to be fresh, got: {freshness.reason}"


def test_promote_current_variant_end_to_end_stays_fresh(tmp_env, sample_profile):
    """The manual 'Approve resume' action (dashboard/CLI path), end to end:
    after promotion, the job must never look stale to either
    verify_resume_freshness or approval.is_current_valid's own JD-change
    comparison."""
    from app.candidate.profile import save_profile
    from app.applications.resume_integrity import verify_resume_freshness
    from app.jobs_repo import get_job

    save_profile(sample_profile)
    job = ingest_and_process(_mock_job("promote-fingerprint-2"))
    optimize_resume(job.id)

    assert promote_current_variant(job.id) is True
    promoted = get_job(job.id)

    assert promoted.resume_jd_fingerprint == promoted.jd_sponsorship_fingerprint
    assert verify_resume_freshness(promoted).fresh
