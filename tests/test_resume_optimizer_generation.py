"""CLAUDE.md Phase 14 sections 75-78: end-to-end resume generation
acceptance -- strong-fit and low-fit fixtures, claim validation, ATS parse
validation, idempotency."""

import json
from pathlib import Path

import pytest

from app.candidate.profile import save_profile
from app.jobs_repo import insert_job
from app.models import ApplicationMode, Job
from app.resume.claim_checker import check_resume_claims
from app.resume_optimizer import repo as ro_repo
from app.resume_optimizer.fingerprint import compute_jd_fingerprint
from app.resume_optimizer.optimizer import generate_optimized_resume_content, optimize_resume
from app.resume_optimizer.jd_analysis import analyze_jd
from app.resume_optimizer.evidence import build_evidence_graph
from app.resume_optimizer.matching import match_requirements


def _strong_fit_job() -> Job:
    return Job(
        title="Backend Software Engineer",
        company="Acme Corp",
        location="Remote",
        description=(
            "Required: Python, FastAPI, PostgreSQL, REST APIs, Docker, CI/CD. "
            "Bachelor's degree in Computer Science required. "
            "Responsibilities include building REST APIs and CI/CD pipelines. "
            "This role offers H-1B sponsorship."
        ),
        mode=ApplicationMode.ASSIST,
    )


def _low_fit_job() -> Job:
    return Job(
        title="Java Backend Engineer",
        company="Acme Corp",
        location="Remote",
        description=(
            "Required: Java, Spring Boot, Kafka, Kubernetes, 7+ years experience. "
            "AWS Certified Solutions Architect required. PhD in Computer Science required. "
            "This role offers H-1B sponsorship."
        ),
        mode=ApplicationMode.ASSIST,
    )


def test_strong_fit_generation_passes_all_gates(tmp_env, sample_profile):
    save_profile(sample_profile)
    job_id = insert_job(_strong_fit_job())
    result = optimize_resume(job_id)

    assert result.status == "READY"
    assert result.quality_report["claim_check"]["passed"] is True
    assert result.quality_report["ats_parseability"]["overall"] == "PASS"
    req = result.quality_report["required_skill_coverage"]
    assert req["directly_verified"] >= 1
    # Every remaining gap is explained, never hidden.
    assert isinstance(result.quality_report["missing_required"], list)


def test_low_fit_never_fabricates_missing_skills(tmp_env, sample_profile):
    save_profile(sample_profile)
    job_id = insert_job(_low_fit_job())
    result = optimize_resume(job_id)

    assert result.quality_report["alignment_label"] == "LOW_ALIGNMENT"
    assert "kafka" in [m.lower() for m in result.quality_report["missing_required"]]
    variant = ro_repo.get_current_variant(job_id)
    resume_text = Path(variant["resume_txt_path"]).read_text().lower()
    for fabricated in ("java", "spring boot", "kubernetes", "phd"):
        assert fabricated not in resume_text, f"'{fabricated}' must never be fabricated onto a mismatched resume"


def test_claim_check_never_bypassed(tmp_env, sample_profile):
    """The optimizer must produce a ResumeContent that independently passes
    the ORIGINAL, unmodified claim checker -- never a parallel/looser check."""
    a = analyze_jd("Backend Software Engineer", _strong_fit_job().description)
    graph = build_evidence_graph(sample_profile)
    matches = match_requirements(a.requirements, graph, sample_profile)
    resume = generate_optimized_resume_content(
        sample_profile, "Backend Software Engineer", _strong_fit_job().description, a, matches
    )
    violations = check_resume_claims(resume, sample_profile)
    assert violations == []


def test_idempotent_same_input_no_duplicate_variant(tmp_env, sample_profile):
    save_profile(sample_profile)
    job_id = insert_job(_strong_fit_job())
    r1 = optimize_resume(job_id)
    r2 = optimize_resume(job_id)
    assert r1.variant_id == r2.variant_id
    assert r2.created is False
    variants = ro_repo.list_variants_for_job(job_id)
    assert len(variants) == 1


def test_jd_change_invalidates_variant(tmp_env, sample_profile):
    from app.pipeline import reanalyze_job

    save_profile(sample_profile)
    job_id = insert_job(_strong_fit_job())
    optimize_resume(job_id)
    assert ro_repo.get_current_variant(job_id)["status"] == "READY"

    reanalyze_job(job_id, new_description=_strong_fit_job().description + " Additional requirement: Kafka.")
    variant = ro_repo.get_current_variant(job_id)
    assert variant["status"] == "STALE"

    result2 = optimize_resume(job_id)
    assert result2.created is True
    assert ro_repo.get_current_variant(job_id)["status"] == "READY"


def test_profile_change_invalidates_variant_identity(tmp_env, sample_profile):
    save_profile(sample_profile)
    job_id = insert_job(_strong_fit_job())
    r1 = optimize_resume(job_id)

    updated = sample_profile.model_copy(deep=True)
    updated.skills.append("kubernetes")
    save_profile(updated)

    r2 = optimize_resume(job_id)
    assert r2.variant_id != r1.variant_id
    assert r2.created is True
    variants = ro_repo.list_variants_for_job(job_id)
    assert len(variants) == 2


def test_variant_uniqueness_index_prevents_duplicate_identity(tmp_env, sample_profile):
    """CLAUDE.md section 72: the database's own unique index is the
    concurrency guard -- a second claim for the identical identity raises
    DuplicateVariantError rather than silently succeeding."""
    save_profile(sample_profile)
    job_id = insert_job(_strong_fit_job())
    result = optimize_resume(job_id)
    fingerprint = compute_jd_fingerprint(_strong_fit_job().title, _strong_fit_job().company, _strong_fit_job().description)
    from app.resume_optimizer.fingerprint import compute_profile_version, OPTIMIZER_VERSION

    profile_version = compute_profile_version(sample_profile)
    with pytest.raises(ro_repo.DuplicateVariantError):
        ro_repo.claim_variant(job_id, fingerprint, profile_version, OPTIMIZER_VERSION)
