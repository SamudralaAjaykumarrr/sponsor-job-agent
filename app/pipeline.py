import json
from pathlib import Path

from app.applications.answers import generate_application_answers
from app.candidate.profile import load_profile
from app.config import MIN_MATCH_SCORE, OUTPUT_DIR
from app.freshness.tracker import compute_age_minutes, compute_freshness
from app.jobs_repo import get_job, insert_job, record_state_change, update_job
from app.matching.compensation import evaluate_compensation
from app.matching.roles import is_target_role
from app.matching.seniority import evaluate_seniority
from app.matching.skills import extract_jd_keywords, match_candidate_skills
from app.models import ApplicationMode, ApplicationState, Job, SponsorshipStatus
from app.resume.claim_checker import check_resume_claims
from app.resume.docx_writer import write_docx
from app.resume.generator import generate_resume_content
from app.resume.pdf_writer import write_pdf
from app.resume.txt_writer import write_txt
from app.scoring.scorer import build_score_breakdown, compute_priority_score, determine_priority_tier
from app.sponsorship.decision import persist_decision
from app.workarrangement.classifier import classify_work_arrangement


def _transition(job_id: int, from_state: ApplicationState, to_state: ApplicationState, **fields) -> None:
    update_job(job_id, application_state=to_state, **fields)
    if from_state != to_state:
        record_state_change(job_id, from_state.value, to_state.value)


def analyze_job(job_id: int) -> Job:
    """Runs classification + scoring on a stored job and applies the hard
    gates, in order: target-role relevance, NO_SPONSORSHIP, seniority,
    compensation, minimum match score. UNKNOWN sponsorship is analyzed but not
    progressed ("do not apply"). CONFIRMED/LIKELY sponsor jobs that pass every
    gate stay ANALYZED, ready for generate_assist_outputs to decide
    READY_TO_APPLY vs REVIEW_REQUIRED."""
    job = get_job(job_id)
    if job is None:
        raise ValueError(f"job {job_id} not found")

    relevant, is_primary = is_target_role(job.title)
    if not relevant:
        _transition(job_id, job.application_state, ApplicationState.SKIPPED,
                    notes="Skipped: not a CS/STEM target role.")
        return get_job(job_id)

    work_arrangement = classify_work_arrangement(job.location, job.description)
    decision = persist_decision(job_id, job.title, job.company, job.description, job.state)
    sponsorship_status, sponsorship_evidence = decision.status, decision.evidence_text
    freshness_tier = compute_freshness(job.published_at, job.first_seen_at)
    freshness_minutes = compute_age_minutes(job.published_at, job.first_seen_at)

    profile = load_profile()
    jd_keywords = extract_jd_keywords(f"{job.title}\n{job.description}")
    match_score, matched, gaps = match_candidate_skills(jd_keywords, profile.skills)

    seniority_ok, seniority_reason, _required_years = evaluate_seniority(job.title, job.description)
    compensation_ok, compensation_reason = evaluate_compensation(job.salary_min, job.salary_max)

    priority_tier = determine_priority_tier(work_arrangement, sponsorship_status)
    priority_score = compute_priority_score(priority_tier, match_score, freshness_tier)
    score_breakdown = build_score_breakdown(
        work_arrangement=work_arrangement,
        sponsorship_status=sponsorship_status,
        priority_tier=priority_tier,
        priority_score=priority_score,
        technical_match_score=match_score,
        matched_skills=matched,
        gap_skills=gaps,
        freshness_tier=freshness_tier,
        freshness_minutes=freshness_minutes,
        seniority_reason=seniority_reason,
        compensation_reason=compensation_reason,
    )

    common_fields = dict(
        work_arrangement=work_arrangement,
        sponsorship_status=sponsorship_status,
        sponsorship_evidence=sponsorship_evidence,
        sponsorship_decision_version=decision.decision_version,
        jd_sponsorship_fingerprint=decision.jd_fingerprint,
        sponsorship_conflict=decision.conflict,
        sponsorship_blocking_reason=decision.blocking_reason,
        freshness_tier=freshness_tier,
        freshness_minutes=freshness_minutes,
        technical_match_score=match_score,
        matched_skills=", ".join(matched),
        gap_skills=", ".join(gaps),
        score_breakdown=json.dumps(score_breakdown),
        priority_tier=priority_tier,
        priority_score=priority_score,
    )

    if sponsorship_status == SponsorshipStatus.NO_SPONSORSHIP:
        _transition(
            job_id, job.application_state, ApplicationState.SKIPPED_NO_SPONSORSHIP,
            notes=f"Hard skip: NO_SPONSORSHIP. {sponsorship_evidence}", **common_fields,
        )
        return get_job(job_id)

    if not seniority_ok:
        _transition(
            job_id, job.application_state, ApplicationState.SKIPPED_SENIORITY,
            notes=f"Skipped: {seniority_reason}", **common_fields,
        )
        return get_job(job_id)

    if not compensation_ok:
        _transition(
            job_id, job.application_state, ApplicationState.SKIPPED_COMPENSATION,
            notes=f"Skipped: {compensation_reason}", **common_fields,
        )
        return get_job(job_id)

    if match_score < MIN_MATCH_SCORE:
        _transition(
            job_id, job.application_state, ApplicationState.SKIPPED_POOR_MATCH,
            notes=f"Skipped: technical match {match_score}% below threshold ({MIN_MATCH_SCORE}%).",
            **common_fields,
        )
        return get_job(job_id)

    if sponsorship_status == SponsorshipStatus.UNKNOWN:
        _transition(
            job_id, job.application_state, ApplicationState.ANALYZED,
            notes="Not progressed: sponsorship UNKNOWN (do not apply per policy).", **common_fields,
        )
        return get_job(job_id)

    _transition(job_id, job.application_state, ApplicationState.ANALYZED, notes=sponsorship_evidence, **common_fields)
    return get_job(job_id)


_TERMINAL_STATES = (ApplicationState.APPLIED, ApplicationState.INTERVIEW, ApplicationState.REJECTED)


def reanalyze_job(
    job_id: int, new_title: str | None = None, new_company: str | None = None, new_description: str | None = None,
) -> Job:
    """JD-change detection + re-classification (CLAUDE.md Phase 7 sections
    24, 57 scenarios 7-8). Only re-runs the full gate pipeline when the
    supplied text actually differs from what's stored -- calling this with
    unchanged text is a no-op (no new decision_version, no state change).
    A job already in a terminal human-driven state (APPLIED/INTERVIEW/
    REJECTED) is never silently moved by a JD edit: the new decision is
    still computed and recorded for audit history, but application_state is
    left untouched -- a human already acted on this job."""
    job = get_job(job_id)
    if job is None:
        raise ValueError(f"job {job_id} not found")

    changes = {}
    if new_title is not None and new_title != job.title:
        changes["title"] = new_title
    if new_company is not None and new_company != job.company:
        changes["company"] = new_company
    if new_description is not None and new_description != job.description:
        changes["description"] = new_description

    if not changes:
        return job

    update_job(job_id, **changes)

    if job.application_state in _TERMINAL_STATES:
        refreshed = get_job(job_id)
        persist_decision(job_id, refreshed.title, refreshed.company, refreshed.description, refreshed.state)
        return get_job(job_id)

    analyzed = analyze_job(job_id)
    return _progress_after_analysis(analyzed)


def _job_output_dir(job_id: int) -> Path:
    d = OUTPUT_DIR / str(job_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def generate_assist_outputs(job_id: int) -> Job:
    """ASSIST mode: generates resume (docx/pdf/txt), job_analysis.json,
    application_answers.json, cover_letter.txt for jobs eligible per
    sponsorship policy (CONFIRMED_SPONSOR or LIKELY_SPONSOR). CONFIRMED jobs
    land on READY_TO_APPLY; LIKELY jobs land on REVIEW_REQUIRED (never
    auto-submitted). Claim violations land on CLAIM_VALIDATION_FAILED instead
    of raising, so batch/autonomous callers can continue processing other
    jobs. Raises only for the programmer-error case of calling this on a job
    that was never sponsorship-eligible in the first place."""
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
        _transition(
            job_id, job.application_state, ApplicationState.CLAIM_VALIDATION_FAILED,
            notes="BLOCKED: resume contained unsupported claims: " + "; ".join(violations),
        )
        return get_job(job_id)

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
        "score_breakdown": json.loads(job.score_breakdown) if job.score_breakdown else {},
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
    final_state = ApplicationState.REVIEW_REQUIRED if review_only else ApplicationState.READY_TO_APPLY
    notes = job.notes
    if review_only:
        notes = (notes + " " if notes else "") + "REVIEW REQUIRED: LIKELY_SPONSOR -- verify sponsorship before applying."

    _transition(
        job_id, job.application_state, final_state,
        resume_docx_path=str(docx_path),
        resume_pdf_path=str(pdf_path),
        resume_txt_path=str(txt_path),
        job_analysis_path=str(job_analysis_path),
        application_answers_path=str(answers_path),
        cover_letter_path=str(cover_letter_path) if cover_letter_path else None,
        notes=notes,
    )
    return get_job(job_id)


def _progress_after_analysis(analyzed: Job) -> Job:
    if analyzed.mode == ApplicationMode.ANALYZE:
        return analyzed

    if analyzed.application_state != ApplicationState.ANALYZED:
        return analyzed  # hard-skipped by a gate above

    if analyzed.sponsorship_status == SponsorshipStatus.UNKNOWN:
        return analyzed  # do not apply -- stays ANALYZED per policy

    if analyzed.mode == ApplicationMode.ASSIST:
        return generate_assist_outputs(analyzed.id)

    # AUTO mode: future use only, not implemented for MVP. Never auto-submit.
    return analyzed


def ingest_and_process(job: Job) -> Job:
    """Full pipeline entry point for a freshly ingested job (manual paste or
    autonomous discovery)."""
    job_id = insert_job(job)
    analyzed = analyze_job(job_id)
    return _progress_after_analysis(analyzed)
