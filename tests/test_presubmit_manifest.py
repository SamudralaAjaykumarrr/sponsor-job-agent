"""Real Provider Execution V1: the provider-neutral PRE-SUBMIT MANIFEST.

Covers everything the brief's PRE-SUBMIT REVIEW section lists, plus the two
properties that keep it safe: it is read-only (never a gate), and it redacts
candidate answer values by default.
"""

import httpx
import pytest

from app.applications.presubmit_manifest import build_manifest, render_text


@pytest.fixture
def _job(tmp_env, sample_profile, monkeypatch):
    from app.candidate.profile import save_profile
    from app.jobs_repo import insert_job, update_job
    from app.models import ApplicationState, Job, SponsorshipStatus

    save_profile(sample_profile)
    counter = {"n": 0}

    def _make(*, provider: str = "greenhouse", with_resume: bool = True, with_cover_letter: bool = False,
              variant_id: str = "") -> int:
        counter["n"] += 1
        job = Job(
            title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
            description="Full-time role. H-1B sponsorship is available.", employment_type="full_time",
            sponsorship_status=SponsorshipStatus.CONFIRMED_SPONSOR, technical_match_score=80.0,
            application_state=ApplicationState.READY_TO_APPLY, provider=provider,
            external_job_id=f"9000{counter['n']}", company_identifier="acme",
            canonical_url=f"https://boards.greenhouse.io/acme/jobs/9000{counter['n']}",
        )
        job_id = insert_job(job)
        updates = {}
        if with_resume:
            job_dir = tmp_env["output_dir"] / str(job_id)
            job_dir.mkdir(parents=True, exist_ok=True)
            resume = job_dir / "resume.pdf"
            resume.write_bytes(b"%PDF-1.4 tailored resume")
            updates["resume_pdf_path"] = str(resume)
        if with_cover_letter:
            job_dir = tmp_env["output_dir"] / str(job_id)
            job_dir.mkdir(parents=True, exist_ok=True)
            cover = job_dir / "cover_letter.txt"
            cover.write_text("Dear hiring manager")
            updates["cover_letter_path"] = str(cover)
        if variant_id:
            updates["promoted_resume_variant_id"] = variant_id
        if updates:
            update_job(job_id, **updates)
        return job_id

    return _make


@pytest.fixture(autouse=True)
def _no_live_greenhouse_calls(monkeypatch):
    """Every manifest build that discovers a Greenhouse form is served a
    real-shaped fixture payload -- no test ever reaches the network."""
    from tests.test_real_provider_execution_adapters import GREENHOUSE_PAYLOAD
    import app.applications.providers_greenhouse as gh

    def factory(*args, **kwargs):
        return httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=GREENHOUSE_PAYLOAD)))

    monkeypatch.setattr(gh, "build_client", factory)


# --- contents -----------------------------------------------------------------

def test_manifest_carries_every_section_the_brief_requires(_job):
    job_id = _job(variant_id="var_77")
    manifest = build_manifest(job_id)
    d = manifest.as_dict()

    # job identity + provider
    assert d["job_id"] == job_id
    assert d["company"] == "Acme Corp"
    assert d["title"] == "Backend Software Engineer"
    assert d["provider"] == "greenhouse"
    assert d["job_identity_fingerprint"]
    # provider capabilities
    assert d["capabilities"]["submission_supported"] is False
    assert d["capabilities"]["confirmation_supported"] is True
    # resume artifact + hash
    resume = d["documents"][0]
    assert resume["kind"] == "RESUME"
    assert resume["exists"] is True
    assert resume["sha256"]
    # mapped answers + unanswered required + form/profile fingerprints
    assert d["answer_count"] > 0
    assert isinstance(d["unanswered_required"], list)
    assert d["form_fingerprint"]
    assert d["profile_fingerprint"]
    # approval / blockers
    assert d["has_approval"] is False
    assert d["active_blocker_code"] == ""
    # authorization summary
    assert isinstance(d["ready_for_approval"], bool)


def test_cover_letter_is_included_only_when_present(_job):
    without = build_manifest(_job(with_cover_letter=False))
    assert [doc.kind for doc in without.documents] == ["RESUME"]
    with_cover = build_manifest(_job(with_cover_letter=True))
    kinds = [doc.kind for doc in with_cover.documents]
    assert kinds == ["RESUME", "COVER_LETTER"]
    assert with_cover.documents[1].sha256


def test_manifest_reports_the_bound_document_when_one_exists(_job):
    from app.applications import document_binding
    from app.jobs_repo import get_job

    job_id = _job(variant_id="var_abc")
    job = get_job(job_id)
    document_binding.record_binding(
        job_id=job_id, document_kind=document_binding.DocumentKind.RESUME,
        artifact_path=job.resume_pdf_path, provider="greenhouse", provider_field_id="resume",
        resume_variant_id="var_abc", verified=True,
    )
    manifest = build_manifest(job_id)
    assert manifest.documents[0].bound_to_field == "resume"
    assert manifest.documents[0].resume_variant_id == "var_abc"
    assert manifest.documents[0].binding_id


def test_unknown_employer_question_appears_as_unanswered_and_blocking(_job):
    job_id = _job()
    manifest = build_manifest(job_id)
    assert any("internal Acme initiative" in label for label in manifest.unanswered_required)
    assert any("required field" in reason for reason in manifest.blocking_reasons)
    assert manifest.ready_for_approval is False


def test_missing_resume_artifact_is_a_blocking_reason(_job):
    manifest = build_manifest(_job(with_resume=False))
    assert manifest.documents[0].exists is False
    assert any("no generated resume artifact" in r for r in manifest.blocking_reasons)
    assert manifest.ready_for_approval is False


def test_provider_without_a_published_schema_reports_candidate_answers_only(_job):
    """Lever publishes no field list -- the manifest must say so rather than
    inventing an employer form."""
    manifest = build_manifest(_job(provider="lever"))
    assert manifest.form_source == "CANDIDATE_PROFILE_ONLY"
    assert manifest.form_field_count == 0
    assert len(manifest.answers) > 0
    assert manifest.capabilities.submission_supported is False


def test_manifest_is_none_for_a_missing_job(tmp_env):
    assert build_manifest(999999) is None


def test_discover_form_false_skips_the_provider_lookup(_job):
    manifest = build_manifest(_job(), discover_form=False)
    assert manifest.form_source == "CANDIDATE_PROFILE_ONLY"
    assert manifest.form_field_count == 0


# --- privacy ------------------------------------------------------------------

def test_answer_values_are_redacted_by_default(_job):
    manifest = build_manifest(_job())
    d = manifest.as_dict()
    values = [a["value"] for a in d["answers"] if a["has_value"]]
    assert values, "expected at least one prepared answer"
    assert all(v == "[redacted]" for v in values)
    assert "test.candidate@example.com" not in str(d)


def test_answer_values_are_shown_only_on_explicit_request(_job):
    d = build_manifest(_job()).as_dict(include_values=True)
    assert "test.candidate@example.com" in str(d)


def test_render_text_redacts_by_default(_job):
    text = render_text(build_manifest(_job()))
    assert "values redacted" in text
    assert "test.candidate@example.com" not in text
    assert "submission_supported    False" in text


# --- approval / staleness ------------------------------------------------------

def test_manifest_reports_a_stale_approval_as_blocking(_job, monkeypatch):
    from app.applications import approval as approval_mod
    from app.applications import repo as executions_repo
    from app.applications.models import ExecutionStatus
    from app.jobs_repo import get_job, update_job

    job_id = _job()
    job = get_job(job_id)
    execution_id = executions_repo.create_execution(job_id, provider="greenhouse", mode="ASSIST")
    executions_repo.update_execution(execution_id, job_id, ExecutionStatus.SUBMISSION_READY,
                                      form_fingerprint="fp-original", answers_version=12,
                                      resume_artifact_hash="hash-original")
    execution = executions_repo.get_execution(execution_id)
    approval_mod._record_approval_row(job, execution, provider_submission_supported=False)

    # The employer changed the form after approval.
    executions_repo.update_execution(execution_id, job_id, ExecutionStatus.SUBMISSION_READY,
                                      form_fingerprint="fp-CHANGED")
    manifest = build_manifest(job_id)
    assert manifest.has_approval is True
    assert manifest.approval_valid is False
    assert any("form changed" in r for r in manifest.approval_stale_reasons)
    assert any("no longer current" in r for r in manifest.blocking_reasons)
    assert manifest.ready_for_approval is False


def test_manifest_reports_an_active_blocker(_job):
    from app.applications import blockers
    from app.applications import repo as executions_repo

    job_id = _job()
    execution_id = executions_repo.create_execution(job_id, provider="greenhouse", mode="ASSIST")
    blockers.raise_blocker(execution_id, job_id, blockers.BlockerCode.NEEDS_CAPTCHA, provider="greenhouse")
    manifest = build_manifest(job_id)
    assert manifest.active_blocker_code == "NEEDS_CAPTCHA"
    assert manifest.active_blocker_title
    assert any("active blocker: NEEDS_CAPTCHA" in r for r in manifest.blocking_reasons)


# --- it is a report, never a gate ---------------------------------------------

def test_manifest_never_authorizes_submission(_job):
    """Even a fully clean manifest must not claim any submission
    authorization for a real provider."""
    manifest = build_manifest(_job())
    assert manifest.capabilities.submission_supported is False
    assert manifest.has_approval is False


def test_building_a_manifest_changes_no_execution_state(_job):
    from app.applications import repo as executions_repo
    from app.applications.models import ExecutionStatus

    job_id = _job()
    execution_id = executions_repo.create_execution(job_id, provider="greenhouse", mode="ASSIST")
    executions_repo.update_execution(execution_id, job_id, ExecutionStatus.SUBMISSION_READY)
    before = executions_repo.get_execution(execution_id)
    build_manifest(job_id)
    after = executions_repo.get_execution(execution_id)
    assert before["status"] == after["status"]
    assert before["updated_at"] == after["updated_at"]
