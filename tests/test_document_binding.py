"""Real Provider Execution V1: durable document-upload binding.

The brief's DOCUMENT UPLOAD requirement -- prove the exact
application-specific resume artifact is the one selected for upload, bound to
job / variant / hash / filename / provider field / timestamp, and never
silently substituted.
"""

import pytest

from app.applications import document_binding
from app.applications.document_binding import DocumentKind


@pytest.fixture
def _job_with_resume(tmp_env, sample_profile):
    from app.candidate.profile import save_profile
    from app.jobs_repo import insert_job, update_job
    from app.models import ApplicationState, Job, SponsorshipStatus

    save_profile(sample_profile)
    counter = {"n": 0}

    def _make(*, variant_id: str = "") -> tuple[int, str]:
        counter["n"] += 1
        job = Job(
            title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
            description="Full-time role. H-1B sponsorship is available.", employment_type="full_time",
            sponsorship_status=SponsorshipStatus.CONFIRMED_SPONSOR, technical_match_score=80.0,
            application_state=ApplicationState.READY_TO_APPLY, provider="greenhouse",
            external_job_id=f"1234{counter['n']}", company_identifier="acme",
        )
        job_id = insert_job(job)
        job_dir = tmp_env["output_dir"] / str(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        resume = job_dir / "resume.pdf"
        resume.write_bytes(b"%PDF-1.4 tailored resume for this job")
        update_job(job_id, resume_pdf_path=str(resume), promoted_resume_variant_id=variant_id)
        return job_id, str(resume)

    return _make


# --- the substitution guard ---------------------------------------------------

def test_artifact_belonging_to_the_job_verifies(_job_with_resume):
    job_id, resume = _job_with_resume()
    check = document_binding.verify_artifact_matches_job(job_id, resume)
    assert check.ok is True
    assert check.sha256
    assert check.filename == "resume.pdf"


def test_another_jobs_resume_is_never_accepted(_job_with_resume):
    """The "never silently substitute another resume" guard."""
    job_a, resume_a = _job_with_resume()
    job_b, _resume_b = _job_with_resume()
    check = document_binding.verify_artifact_matches_job(job_b, resume_a)
    assert check.ok is False
    assert f"does not belong to job {job_b}" in check.reason


def test_nested_optimizer_variant_layout_is_accepted(tmp_env, _job_with_resume):
    """`output/<job_id>/optimized/<variant_id>/resume.pdf` -- the resume
    optimizer's real layout, which an exact immediate-parent check would
    wrongly reject (a real integration bug this project already fixed once)."""
    job_id, _resume = _job_with_resume()
    nested = tmp_env["output_dir"] / str(job_id) / "optimized" / "var_abc" / "resume.pdf"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_bytes(b"%PDF-1.4 one-page variant")
    assert document_binding.verify_artifact_matches_job(job_id, str(nested)).ok is True


def test_missing_or_empty_artifact_never_verifies(_job_with_resume, tmp_path):
    job_id, _resume = _job_with_resume()
    assert document_binding.verify_artifact_matches_job(job_id, "").ok is False
    assert document_binding.verify_artifact_matches_job(job_id, str(tmp_path / "gone.pdf")).ok is False


# --- the durable binding ------------------------------------------------------

def test_binding_records_every_field_the_brief_requires(_job_with_resume):
    job_id, resume = _job_with_resume(variant_id="var_9f21")
    check = document_binding.verify_artifact_matches_job(job_id, resume)
    row = document_binding.record_binding(
        job_id=job_id, document_kind=DocumentKind.RESUME, artifact_path=resume, provider="greenhouse",
        execution_id="exec_1", session_id="sess_1", resume_variant_id="var_9f21",
        provider_field_id="resume", provider_field_label="Resume/CV",
        checkpoint="browser_assist:form_fingerprint=abc123", verified=True, artifact_sha256=check.sha256,
    )
    assert row["job_id"] == job_id
    assert row["resume_variant_id"] == "var_9f21"
    assert row["artifact_sha256"] == check.sha256
    assert row["artifact_filename"] == "resume.pdf"
    assert row["provider_field_id"] == "resume"
    assert row["checkpoint"] == "browser_assist:form_fingerprint=abc123"
    assert row["created_at"]
    assert row["verified"] == 1


def test_verified_is_stored_as_an_integer_not_a_python_bool(_job_with_resume):
    """CLAUDE.md's Phase 9 boolean-coercion rule -- psycopg maps a Python
    bool to Postgres `boolean`, which conflicts with this INTEGER column."""
    job_id, resume = _job_with_resume()
    row = document_binding.record_binding(
        job_id=job_id, document_kind=DocumentKind.RESUME, artifact_path=resume, verified=True,
    )
    assert row["verified"] == 1
    assert isinstance(row["verified"], int)


def test_bindings_are_append_only(_job_with_resume):
    job_id, resume = _job_with_resume()
    first = document_binding.record_binding(
        job_id=job_id, document_kind=DocumentKind.RESUME, artifact_path=resume, provider_field_id="resume")
    second = document_binding.record_binding(
        job_id=job_id, document_kind=DocumentKind.RESUME, artifact_path=resume, provider_field_id="resume")
    assert first["binding_id"] != second["binding_id"]
    rows = document_binding.list_bindings_for_job(job_id)
    assert len(rows) == 2
    assert document_binding.latest_binding(job_id, DocumentKind.RESUME)["binding_id"] == second["binding_id"]


def test_cover_letter_is_bound_separately_from_the_resume(_job_with_resume, tmp_env):
    job_id, resume = _job_with_resume()
    cover = tmp_env["output_dir"] / str(job_id) / "cover_letter.txt"
    cover.write_text("Dear hiring manager")
    document_binding.record_binding(job_id=job_id, document_kind=DocumentKind.RESUME, artifact_path=resume)
    document_binding.record_binding(
        job_id=job_id, document_kind=DocumentKind.COVER_LETTER, artifact_path=str(cover))
    assert document_binding.latest_binding(job_id, DocumentKind.RESUME)["artifact_filename"] == "resume.pdf"
    assert document_binding.latest_binding(
        job_id, DocumentKind.COVER_LETTER)["artifact_filename"] == "cover_letter.txt"


def test_lookups_by_execution_and_session(_job_with_resume):
    job_id, resume = _job_with_resume()
    document_binding.record_binding(job_id=job_id, document_kind=DocumentKind.RESUME, artifact_path=resume,
                                     execution_id="exec_x", session_id="sess_x")
    assert len(document_binding.list_bindings_for_execution("exec_x")) == 1
    assert len(document_binding.list_bindings_for_session("sess_x")) == 1
    assert document_binding.list_bindings_for_execution("exec_other") == []


def test_record_binding_safe_never_raises(_job_with_resume):
    """The write path is best-effort: an audit-log failure must never break
    a real upload."""
    assert document_binding.record_binding_safe(job_id=None, document_kind="RESUME",
                                                 artifact_path="/nope") is None


# --- doctor integration -------------------------------------------------------

def test_doctor_flags_a_binding_pointing_at_another_jobs_resume(_job_with_resume):
    from app.applications.doctor import run_doctor

    job_a, resume_a = _job_with_resume()
    job_b, _ = _job_with_resume()
    document_binding.record_binding(job_id=job_b, document_kind=DocumentKind.RESUME, artifact_path=resume_a)
    issues = [i for i in run_doctor().issues if i.check == "document_binding_wrong_job"]
    assert len(issues) == 1
    assert issues[0].severity == "serious"


def test_doctor_is_quiet_for_a_correct_binding(_job_with_resume):
    from app.applications.doctor import run_doctor

    job_id, resume = _job_with_resume()
    document_binding.record_binding(job_id=job_id, document_kind=DocumentKind.RESUME, artifact_path=resume)
    issues = [i for i in run_doctor().issues if i.check.startswith("document_binding")]
    assert issues == []


def test_doctor_flags_a_binding_whose_execution_belongs_to_another_job(_job_with_resume):
    from app.applications import repo as executions_repo
    from app.applications.doctor import run_doctor

    job_a, resume_a = _job_with_resume()
    job_b, resume_b = _job_with_resume()
    execution_id = executions_repo.create_execution(job_b, provider="greenhouse", mode="ASSIST")
    document_binding.record_binding(job_id=job_a, document_kind=DocumentKind.RESUME, artifact_path=resume_a,
                                     execution_id=execution_id)
    issues = [i for i in run_doctor().issues if i.check == "document_binding_execution_job_mismatch"]
    assert len(issues) == 1
