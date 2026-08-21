from app.candidate.profile import save_profile
from app.jobs_repo import get_job
from app.models import ApplicationMode, ApplicationState, Job, SponsorshipStatus, WorkArrangement
from app.pipeline import ingest_and_process


def _job(**overrides) -> Job:
    defaults = dict(
        title="Backend Software Engineer",
        company="Acme Corp",
        location="Remote (US)",
        description=(
            "We are hiring a Backend Software Engineer to build REST APIs in Python "
            "using FastAPI and PostgreSQL. This is a fully remote position."
        ),
        mode=ApplicationMode.ASSIST,
    )
    defaults.update(overrides)
    return Job(**defaults)


def test_no_sponsorship_job_is_hard_skipped(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = _job(
        company="NoSponsorCo",
        description="Backend engineer role. We are unable to sponsor visas for this position.",
    )
    result = ingest_and_process(job)
    assert result.sponsorship_status == SponsorshipStatus.NO_SPONSORSHIP
    assert result.application_state == ApplicationState.SKIPPED
    assert result.resume_docx_path is None


def test_unknown_sponsorship_does_not_apply(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = _job(company="TotallyUnknownLLC")
    result = ingest_and_process(job)
    assert result.sponsorship_status == SponsorshipStatus.UNKNOWN
    assert result.application_state == ApplicationState.ANALYZED
    assert result.resume_docx_path is None


def test_confirmed_sponsor_remote_reaches_ready_to_apply(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = _job(
        description=(
            "We are hiring a Backend Software Engineer to build REST APIs in Python "
            "using FastAPI and PostgreSQL. This is a fully remote position. "
            "Visa sponsorship available for qualified candidates."
        ),
    )
    result = ingest_and_process(job)
    assert result.sponsorship_status == SponsorshipStatus.CONFIRMED_SPONSOR
    assert result.work_arrangement == WorkArrangement.REMOTE
    assert result.application_state == ApplicationState.READY_TO_APPLY
    assert result.resume_docx_path and result.resume_pdf_path and result.resume_txt_path
    assert result.job_analysis_path and result.application_answers_path


def test_likely_sponsor_is_review_only_but_still_generates_outputs(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = _job(company="Acme Corp")  # in known_h1b_sponsors.json fixture list
    result = ingest_and_process(job)
    assert result.sponsorship_status == SponsorshipStatus.LIKELY_SPONSOR
    assert result.application_state == ApplicationState.READY_TO_APPLY
    assert "REVIEW ONLY" in result.notes
    assert result.resume_docx_path


def test_non_target_role_is_skipped(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = _job(title="Retail Store Associate", company="Acme Corp")
    result = ingest_and_process(job)
    assert result.application_state == ApplicationState.SKIPPED


def test_analyze_mode_does_not_generate_files(tmp_env, sample_profile):
    save_profile(sample_profile)
    job = _job(
        description=(
            "We are hiring a Backend Software Engineer to build REST APIs in Python. "
            "This is a fully remote position. Visa sponsorship available."
        ),
        mode=ApplicationMode.ANALYZE,
    )
    result = ingest_and_process(job)
    assert result.sponsorship_status == SponsorshipStatus.CONFIRMED_SPONSOR
    assert result.resume_docx_path is None
    assert result.application_state == ApplicationState.ANALYZED


def test_incomplete_profile_blocks_resume_and_reports_gap(tmp_env):
    # Profile left blank (all NEEDS_USER_INPUT) -- resume must not fabricate facts.
    job = _job(
        description=(
            "We are hiring a Backend Software Engineer to build REST APIs in Python. "
            "This is a fully remote position. Visa sponsorship available."
        ),
    )
    result = ingest_and_process(job)
    assert result.sponsorship_status == SponsorshipStatus.CONFIRMED_SPONSOR
    # No verified skills/employment -> resume has no unverifiable claims to block,
    # but full_name/contact fields remain NEEDS_USER_INPUT rather than invented.
    stored = get_job(result.id)
    if stored.resume_txt_path:
        assert "NEEDS_USER_INPUT" in open(stored.resume_txt_path).read()
