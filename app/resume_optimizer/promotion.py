"""Shared "promote this resume_variants row onto the job" helper -- the
exact write app.agent.orchestrator._run_resume_stage already performed
inline, factored out so a manual "Approve resume" dashboard action (used
when Apply/Automation Settings V1's Auto-approve resume is OFF) can reuse
the identical, unmodified write path rather than a second, parallel one."""

from app.jobs_repo import get_job, update_job
from app.resume_optimizer.models import ResumeVariantStatus
from app.resume_optimizer.repo import get_current_variant


def promote_variant(job_id: int, variant: dict) -> None:
    update_job(
        job_id,
        resume_docx_path=variant["resume_docx_path"], resume_pdf_path=variant["resume_pdf_path"],
        resume_txt_path=variant["resume_txt_path"], resume_jd_fingerprint=variant["jd_fingerprint"],
        promoted_resume_variant_id=variant["variant_id"],
    )


def promote_current_variant(job_id: int) -> bool:
    """The manual "Approve resume" action -- never gated by the Auto-approve
    resume setting (matching this project's "manual action is never gated
    by an automation flag" convention, e.g. APPLICATION_AUTO_PREPARE_ENABLED/
    RESUME_OPTIMIZATION_ENABLED never gating the manual dashboard button/
    CLI). Only ever promotes a variant that has already independently passed
    every quality gate (READY status implies claim-check + one-page +
    ATS-parse all already passed) -- this function adds no additional
    check of its own, and never promotes a REVIEW_REQUIRED/CLAIM_CHECK_FAILED/
    ATS_PARSE_FAILED/GENERATING/STALE variant."""
    if get_job(job_id) is None:
        return False
    variant = get_current_variant(job_id)
    if variant is None:
        return False
    if variant.get("status") != ResumeVariantStatus.READY.value or variant.get("page_count") != 1:
        return False
    promote_variant(job_id, variant)
    return True
