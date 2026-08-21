import json
from pathlib import Path

from app.applications.answers import generate_application_answers
from app.candidate.profile import load_profile
from app.config import OUTPUT_DIR
from app.freshness.tracker import compute_freshness
from app.jobs_repo import get_job, insert_job, update_job
from app.matching.roles import is_target_role
from app.matching.skills import extract_jd_keywords, match_candidate_skills
from app.models import ApplicationMode, ApplicationState, Job, SponsorshipStatus
from app.resume.claim_checker import check_resume_claims
from app.resume.docx_writer import write_docx
from app.resume.generator import generate_resume_content
from app.resume.pdf_writer import write_pdf
from app.resume.txt_writer import write_txt
from app.scoring.scorer import compute_priority_score, determine_priority_tier
from app.sponsorship.classifier import classify_sponsorship
from app.workarrangement.classifier import classify_work_arrangement


def analyze_job(job_id: int) -> Job:
    """Runs classification + scoring on a stored job. Applies the hard gates:
    non-target-role and NO_SPONSORSHIP jobs are skipped and not processed further.
    """
    job = get_job(job_id)
    if job is None:
        raise ValueError(f"job {job_id} not found")

    relevant, is_primary = is_target_role(job.title)
    if not relevant:
        update_job(job_id, application_state=ApplicationState.SKIPPED,
                   notes="Skipped: not a CS/STEM target role.")
        return get_job(job_id)

    work_arrangement = classify_work_arrangement(job.location, job.description)
    sponsorship_status, sponsorship_evidence = classify_sponsorship(job.description, job.company)
    freshness_tier = compute_freshness(job.published_at, job.first_seen_at)

    profile = load_profile()
    jd_keywords = extract_jd_keywords(f"{job.title}\n{job.description}")
    match_score, matched, gaps = match_candidate_skills(jd_keywords, profile.skills)

    priority_tier = determine_priority_tier(work_arrangement, sponsorship_status)
    priority_score = compute_priority_score(priority_tier, match_score, freshness_tier)

    if sponsorship_status == SponsorshipStatus.NO_SPONSORSHIP:
        state = ApplicationState.SKIPPED
        notes = f"Skipped: NO_SPONSORSHIP. {sponsorship_evidence}"
    elif sponsorship_status == SponsorshipStatus.UNKNOWN:
        state = ApplicationState.ANALYZED
        notes = "Not progressed: sponsorship UNKNOWN (do not apply per policy)."
    else:
        state = ApplicationState.ANALYZED
        notes = sponsorship_evidence

    update_job(
        job_id,
        work_arrangement=work_arrangement,
        sponsorship_status=sponsorship_status,
        sponsorship_evidence=sponsorship_evidence,
        freshness_tier=freshness_tier,
        technical_match_score=match_score,
        matched_skills=", ".join(matched),
        gap_skills=", ".join(gaps),
        priority_tier=priority_tier,
        priority_score=priority_score,
        application_state=state,
        notes=notes,
    )
    return get_job(job_id)


def _job_output_dir(job_id: int) -> Path:
    d = OUTPUT_DIR / str(job_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def generate_assist_outputs(job_id: int) -> Job:
    """ASSIST mode: generates resume (docx/pdf/txt), job_analysis.json,
    application_answers.json, cover_letter.txt, and marks READY_TO_APPLY --
    only for jobs eligible per sponsorship policy (CONFIRMED_SPONSOR or
    LIKELY_SPONSOR). Refuses (raises) for NO_SPONSORSHIP/UNKNOWN jobs."""
    job = get_job(job_id)
    if job is None:
        raise ValueError(f"job {job_id} not found")

    if job.sponsorship_status not in (SponsorshipStatus.CONFIRMED_SPONSOR, SponsorshipStatus.LIKELY_SPONSOR):
        raise ValueError(
            f"Refusing to generate application outputs for job {job_id}: "
            f"sponsorship_status={job.sponsorship_status} is not eligible."
        )

    profile = load_profile()
    resume = generate_resume_content(profile, job.title, job.description)

    violations = check_resume_claims(resume, profile)
    if violations:
        update_job(
            job_id,
            application_state=ApplicationState.ANALYZED,
            notes="BLOCKED: resume contained unsupported claims: " + "; ".join(violations),
        )
        raise ValueError("Resume generation blocked -- unsupported claims: " + "; ".join(violations))

    out_dir = _job_output_dir(job_id)

    docx_path = write_docx(resume, out_dir / "resume.docx")
    pdf_path = write_pdf(resume, out_dir / "resume.pdf")
    txt_path = write_txt(resume, out_dir / "resume.txt")

    job_analysis = {
        "job_id": job_id,
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "work_arrangement": job.work_arrangement,
        "sponsorship_status": job.sponsorship_status,
        "sponsorship_evidence": job.sponsorship_evidence,
        "freshness_tier": job.freshness_tier,
        "technical_match_score": job.technical_match_score,
        "matched_skills": job.matched_skills,
        "gap_skills": job.gap_skills,
        "priority_tier": job.priority_tier,
        "priority_score": job.priority_score,
    }
    job_analysis_path = out_dir / "job_analysis.json"
    job_analysis_path.write_text(json.dumps(job_analysis, indent=2, default=str))

    answers = generate_application_answers(profile, job.title, job.company)
    answers_path = out_dir / "application_answers.json"
    answers_path.write_text(json.dumps(answers, indent=2, default=str))

    cover_letter_path = None
    if resume.experience or resume.projects:
        cover_letter_path = out_dir / "cover_letter.txt"
        top_skill = resume.skills_ordered[0] if resume.skills_ordered else "NEEDS_USER_INPUT"
        cover_letter_path.write_text(
            f"Dear Hiring Team at {job.company},\n\n"
            f"I am writing to apply for the {job.title} position. {resume.summary}\n\n"
            f"I would welcome the opportunity to bring my experience in {top_skill} to your team.\n\n"
            f"Sincerely,\n{resume.full_name}\n"
        )

    review_only = job.sponsorship_status == SponsorshipStatus.LIKELY_SPONSOR
    notes = job.notes
    if review_only:
        notes = (notes + " " if notes else "") + "REVIEW ONLY: LIKELY_SPONSOR -- verify sponsorship before applying."

    update_job(
        job_id,
        resume_docx_path=str(docx_path),
        resume_pdf_path=str(pdf_path),
        resume_txt_path=str(txt_path),
        job_analysis_path=str(job_analysis_path),
        application_answers_path=str(answers_path),
        cover_letter_path=str(cover_letter_path) if cover_letter_path else None,
        application_state=ApplicationState.READY_TO_APPLY,
        notes=notes,
    )
    return get_job(job_id)


def ingest_and_process(job: Job) -> Job:
    """Full pipeline entry point for a freshly ingested job."""
    job_id = insert_job(job)
    analyzed = analyze_job(job_id)

    if analyzed.mode == ApplicationMode.ANALYZE:
        return analyzed

    if analyzed.application_state == ApplicationState.SKIPPED:
        return analyzed

    if analyzed.sponsorship_status == SponsorshipStatus.UNKNOWN:
        return analyzed  # do not apply -- stays ANALYZED

    if analyzed.mode == ApplicationMode.ASSIST:
        return generate_assist_outputs(job_id)

    # AUTO mode: future use only, not implemented for MVP. Never auto-submit.
    return analyzed
