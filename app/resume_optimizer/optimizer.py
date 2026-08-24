"""Resume optimizer orchestration (CLAUDE.md Phase 14 sections 1, 21-27,
33, 58, 72). Ties JD analysis -> evidence matching -> truthful bullet/skill
selection -> claim validation -> ATS parse validation -> quality diagnostics
-> persistence, with idempotent, concurrency-safe variant creation.

The truthfulness firewall is `app.resume.claim_checker.check_resume_claims`
(unchanged, Phase 1 code) -- this module NEVER bypasses it. Every resume
this module writes is built exclusively from `ResumeContent` objects whose
skills/bullets/employers/projects/education are taken verbatim from the
verified `CandidateProfile` (never rewritten text, never invented content)."""

from dataclasses import dataclass
from pathlib import Path

from app.candidate.profile import load_profile
from app.candidate.schema import CandidateProfile
from app.config import OUTPUT_DIR
from app.jobs_repo import get_job
from app.resume.claim_checker import check_resume_claims
from app.resume.generator import EducationBlock, ExperienceBlock, ProjectBlock, ResumeContent
from app.resume_optimizer import ats_parse, repo
from app.resume_optimizer.one_page import enforce_one_page
from app.resume_optimizer.evidence import build_evidence_graph
from app.resume_optimizer.fingerprint import compute_artifact_hash, compute_jd_fingerprint, compute_profile_version, OPTIMIZER_VERSION
from app.resume_optimizer.jd_analysis import analyze_jd
from app.resume_optimizer.matching import match_requirements
from app.resume_optimizer.models import EvidenceGraph, JDAnalysisResult, MatchStatus, RequirementCategory, RequirementMatch, ResumeVariantStatus
from app.resume_optimizer.quality import compute_quality
from app.resume_optimizer import relevance as relevance_mod
from app.resume_optimizer.recency import recency_rank
from app.resume_optimizer.role_classification import build_target_role, classify_role
from app.models import utcnow


@dataclass
class OptimizeResult:
    variant_id: str
    status: str
    created: bool  # False when an identical, already-READY variant was reused
    quality_report: dict | None = None
    reason: str = ""


def _matched_requirement_texts(matches: list[RequirementMatch], statuses: tuple[MatchStatus, ...]) -> list[str]:
    return [m.requirement.text for m in matches if m.status in statuses]


_SKILL_GAP_CATEGORIES = (
    RequirementCategory.LANGUAGE, RequirementCategory.FRAMEWORK, RequirementCategory.DATABASE,
    RequirementCategory.CLOUD, RequirementCategory.DEVOPS, RequirementCategory.MESSAGING,
    RequirementCategory.TESTING, RequirementCategory.SECURITY, RequirementCategory.ARCHITECTURE,
    RequirementCategory.FRONTEND, RequirementCategory.BACKEND, RequirementCategory.DATA_ML,
    RequirementCategory.TOOL, RequirementCategory.METHODOLOGY, RequirementCategory.OBSERVABILITY,
)


def generate_optimized_resume_content(
    profile: CandidateProfile, job_title: str, job_description: str,
    jd_analysis: JDAnalysisResult, matches: list[RequirementMatch], graph: EvidenceGraph | None = None,
) -> ResumeContent:
    """CLAUDE.md sections 21-27, JD intelligence v3: strongest truthful
    alignment -- reorders and selects only verified content, never invents
    any. `graph` is optional (rebuilt from `profile` when omitted, e.g. by a
    direct caller that doesn't already have one) so `optimize_resume()` can
    pass its already-computed graph without a second, redundant build."""
    graph = graph if graph is not None else build_evidence_graph(profile)
    role = classify_role(job_title, jd_analysis, graph)
    relevance = relevance_mod.build_relevance_model(jd_analysis, matches, graph, role, job_description)

    matched_required = _matched_requirement_texts(matches, (MatchStatus.MATCHED,))

    # Skills ordering (section 25, role-aware in v3): verified skills ranked
    # by relevance-model weight (required > preferred > role-boosted
    # category > unrelated), ties broken by original profile order --
    # never a skill outside profile.skills.
    verified_skills = [s for s in profile.skills if s and s != "NEEDS_USER_INPUT"]
    skills_ordered = sorted(
        verified_skills,
        key=lambda s: (-relevance_mod.score_skill(s, relevance), verified_skills.index(s)),
    )

    gap_skills = [
        m.requirement.text for m in matches
        if m.requirement.category in _SKILL_GAP_CATEGORIES and m.status == MatchStatus.MISSING
    ]

    # Experience ordering + bullet selection (sections 21-24, JD
    # intelligence v3): rank employers by the shared relevance model
    # (required/responsibility/domain/recency/impact/keyword-weighted, never
    # just a raw matched-term count), select bullets via non-redundant
    # greedy coverage -- preserving employer/title/dates/chronology of the
    # entries actually included.
    recency = recency_rank(profile.employment)
    experience_sorted = sorted(
        profile.employment,
        key=lambda e: relevance_mod.score_experience_entry(
            list(e.verified_bullets), list(e.skills_used), relevance, recency.get(e.company, 0.0),
        ),
        reverse=True,
    )
    experience = [
        ExperienceBlock(
            company=e.company, title=e.title, start_date=e.start_date, end_date=e.end_date,
            location=e.location, bullets=relevance_mod.select_bullets(list(e.verified_bullets), relevance, cap=5),
        )
        for e in experience_sorted
    ]

    projects_sorted = sorted(
        profile.projects,
        key=lambda p: relevance_mod.score_project(list(p.verified_bullets), list(p.skills_used), relevance),
        reverse=True,
    )
    projects = [
        ProjectBlock(
            name=p.name, description=p.description,
            bullets=relevance_mod.select_bullets(list(p.verified_bullets), relevance, cap=4), url=p.url,
        )
        for p in projects_sorted[:3]
    ]

    education = [
        EducationBlock(school=ed.school, degree=ed.degree, field_of_study=ed.field_of_study, graduation_date=ed.graduation_date)
        for ed in profile.education
    ]

    years = profile.standard_answers.years_of_experience
    years_str = f"{years:g} years" if years is not None else "NEEDS_USER_INPUT"
    top_matched = matched_required[:5] or skills_ordered[:5]
    top_str = ", ".join(top_matched) if top_matched else "NEEDS_USER_INPUT"
    # CLAUDE.md section 1/89: the summary never echoes the raw JD title/role
    # name -- doing so (e.g. "targeting Java Backend Engineer") could read as
    # an implied skill match even when it is not one (a real issue this
    # phase's own low-fit acceptance test caught: a Java-mismatched JD's
    # title would otherwise appear verbatim in a Python candidate's summary).
    # Only verified skills ever appear in the summary text.
    summary = f"Software engineer with {years_str} of experience; verified strengths include {top_str}."

    # Target role / headline (resume structure, JD intelligence v3): a
    # forward-looking "what I'm applying for" line, built entirely from a
    # hardcoded safe role family + optionally a verified language + a
    # verified domain -- never the raw JD title, never a claim of prior
    # employment. See app.resume_optimizer.role_classification
    # .build_target_role's docstring for the fabrication risk this design
    # prevents (the same one the summary rule above already guards against).
    target_role = build_target_role(job_title, jd_analysis, graph, role)

    return ResumeContent(
        full_name=profile.contact.full_name, email=profile.contact.email, phone=profile.contact.phone,
        location=f"{profile.contact.city}, {profile.contact.state}", linkedin_url=profile.contact.linkedin_url,
        github_url=profile.contact.github_url, portfolio_url=profile.contact.portfolio_url,
        summary=summary, skills_ordered=skills_ordered, experience=experience, projects=projects,
        education=education, gap_skills=gap_skills, target_role=target_role,
    )


def _variant_dir(job_id: int, variant_id: str) -> Path:
    d = OUTPUT_DIR / str(job_id) / "optimized" / variant_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def optimize_resume(job_id: int, *, force: bool = False) -> OptimizeResult:
    """CLAUDE.md sections 37, 58, 72: idempotent + concurrency-safe. Same
    job/JD-fingerprint/profile-version/optimizer-version never regenerates
    an identical artifact, and two concurrent callers for the identical
    identity never both create a "current" row -- the database's own
    unique index is the serialization point, not an app-level
    check-then-insert. `force=True` skips the fast "already READY, return
    as-is" shortcut and always recomputes JD analysis/evidence matching/
    resume content/claim-check/ATS-parse from scratch -- useful after a
    code change to this module -- but if the recomputed identity is still
    identical to an existing row, that existing row is what gets returned
    (the unique index does not allow a genuine duplicate row for an
    unchanged identity, by design)."""
    job = get_job(job_id)
    if job is None:
        raise ValueError(f"job {job_id} not found")

    profile = load_profile()
    jd_fingerprint = compute_jd_fingerprint(job.title, job.company, job.description)
    profile_version = compute_profile_version(profile)

    if not force:
        existing = repo.get_variant_by_identity(job_id, jd_fingerprint, profile_version, OPTIMIZER_VERSION)
        if existing and existing["status"] == ResumeVariantStatus.READY.value:
            report = repo.get_quality_report(existing["variant_id"])
            return OptimizeResult(
                variant_id=existing["variant_id"], status=existing["status"], created=False,
                quality_report=report["report"] if report else None, reason="identical variant already READY",
            )

    jd_analysis = analyze_jd(job.title, job.description)
    repo.save_jd_analysis(job_id, jd_fingerprint, jd_analysis)

    graph = build_evidence_graph(profile)
    matches = match_requirements(jd_analysis.requirements, graph, profile)

    try:
        claimed = repo.claim_variant(job_id, jd_fingerprint, profile_version, OPTIMIZER_VERSION)
    except repo.DuplicateVariantError:
        existing = repo.get_variant_by_identity(job_id, jd_fingerprint, profile_version, OPTIMIZER_VERSION)
        if existing:
            report = repo.get_quality_report(existing["variant_id"])
            return OptimizeResult(
                variant_id=existing["variant_id"], status=existing["status"], created=False,
                quality_report=report["report"] if report else None,
                reason="concurrent generation already claimed this exact identity",
            )
        raise

    variant_id = claimed["variant_id"]
    resume = generate_optimized_resume_content(profile, job.title, job.description, jd_analysis, matches, graph)

    # Content-level truthfulness gate runs on the FULL, uncompressed resume
    # first: one_page.enforce_one_page() only ever removes/shortens content,
    # never adds any, so if the full resume is truthful, every compressed
    # subset it might produce is too (CLAUDE.md Phase 14's claim_checker
    # remains the single, unmodified firewall -- see app.resume_optimizer.
    # one_page's module docstring for why compression is designed around it).
    violations = check_resume_claims(resume, profile)

    out_dir = _variant_dir(job_id, variant_id)
    # JD-matched requirement text doubles as the "never remove this skill
    # during one-page compression" protected set -- the same signal
    # generate_optimized_resume_content() above used to prioritize skill
    # ordering (section 25).
    protected_lower = {t.lower() for t in _matched_requirement_texts(matches, (MatchStatus.MATCHED,))}

    # JD intelligence v3: overflow removal ranks by the SAME weighted
    # relevance model (required/responsibility/domain/recency/impact/
    # keyword, non-redundancy) that selected the resume's initial content
    # above, instead of one_page's own cruder unweighted fallback.
    role = classify_role(job.title, jd_analysis, graph)
    relevance_model = relevance_mod.build_relevance_model(jd_analysis, matches, graph, role, job.description)
    entry_recency = recency_rank(profile.employment)

    one_page_result = enforce_one_page(
        resume, out_dir, protected_skills_lower=protected_lower,
        relevance_weights=relevance_model.weights, entry_recency=entry_recency,
    )
    docx_path, pdf_path, txt_path = one_page_result.docx_path, one_page_result.pdf_path, one_page_result.txt_path
    resume = one_page_result.resume  # the FINAL (possibly compressed) content actually rendered

    ats_report = ats_parse.validate_all(docx_path, pdf_path, txt_path, resume)
    artifact_hash = compute_artifact_hash(txt_path.read_bytes())

    if violations:
        status = ResumeVariantStatus.CLAIM_CHECK_FAILED
    elif not one_page_result.one_page:
        # CLAUDE.md one-click-agent section 8: a page count that still can't
        # be safely brought to 1 within the bounded compression ladder is an
        # honest REVIEW_REQUIRED, never a tiny unreadable render.
        status = ResumeVariantStatus.REVIEW_REQUIRED
    elif ats_report.overall.value == "FAIL":
        status = ResumeVariantStatus.ATS_PARSE_FAILED
    else:
        status = ResumeVariantStatus.READY

    repo.finalize_variant(
        variant_id, status=status.value, resume_docx_path=str(docx_path), resume_pdf_path=str(pdf_path),
        resume_txt_path=str(txt_path), resume_artifact_hash=artifact_hash, make_current=True,
        page_count=one_page_result.page_count, compression_steps_applied=one_page_result.compression_steps_applied,
        compression_log=one_page_result.compression_log,
    )
    repo.save_evidence_links(variant_id, matches)

    quality_report = compute_quality(
        job_id=job_id, jd_fingerprint=jd_fingerprint, resume_artifact_hash=artifact_hash, job_title=job.title,
        jd_analysis=jd_analysis, matches=matches, resume=resume, ats_report=ats_report,
        claim_violations=violations, optimizer_version=OPTIMIZER_VERSION, generated_at=utcnow(),
        candidate_domains=graph.domains, candidate_salary_min_usd=profile.preferences.salary_min_usd,
    )
    report_dict = quality_report.as_dict()
    repo.save_quality_report(variant_id, job_id, jd_fingerprint, report_dict)

    return OptimizeResult(variant_id=variant_id, status=status.value, created=True, quality_report=report_dict)
