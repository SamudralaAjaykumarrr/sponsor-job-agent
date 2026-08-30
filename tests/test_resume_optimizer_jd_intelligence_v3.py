"""JD intelligence v3 regression suite (branch feat/resume-jd-intelligence-v3).

Uses only clearly-synthetic JD text and clearly-synthetic candidate profiles
(mirrors tests/test_resume_optimizer_airbnb_regression.py's convention) --
never real candidate data. Covers: role-aware selection (backend/payments/
platform/full-stack/QA-SDET), alternative-language OR-form requirements,
unsupported Go/K6/Locust, a 7-year requirement, an education requirement
with "or equivalent experience", observability/testing terminology
expansion, a fraud/ML nice-to-have, semantic requirement deduplication,
compensation parsing, the truthful TARGET ROLE / headline contract
(distinct from verified employment titles), and determinism/idempotency.
"""

from app.candidate.schema import CandidateProfile
from app.resume.claim_checker import _validate_target_role, check_resume_claims
from app.resume_optimizer.evidence import build_evidence_graph
from app.resume_optimizer.jd_analysis import analyze_jd
from app.resume_optimizer.matching import match_requirements
from app.resume_optimizer.models import MatchStatus, RequirementCategory, RequirementPriority
from app.resume_optimizer.optimizer import generate_optimized_resume_content, optimize_resume
from app.resume_optimizer.relevance import RelevanceModel, select_bullets
from app.resume_optimizer.role_classification import RoleArchetype, build_target_role, classify_role


def _profile(**overrides) -> CandidateProfile:
    base = {
        "contact": {
            "full_name": "Test Candidate", "email": "test.candidate@example.com",
            "phone": "555-000-3333", "city": "Denver", "state": "CO",
            "linkedin_url": "", "github_url": "", "portfolio_url": "",
        },
        "employment": [],
        "skills": [],
        "projects": [],
        "education": [
            {"school": "State University", "degree": "B.S.", "field_of_study": "Computer Science", "graduation_date": "2020-05"}
        ],
        "work_authorization": {
            "current_status": "F-1 OPT", "requires_sponsorship": True,
            "sponsorship_type_needed": "H-1B", "years_us_experience": 3,
        },
        "preferences": {
            "relocation_open": False, "preferred_locations": ["Remote"],
            "salary_min_usd": 110000, "salary_preference_notes": "",
            "work_arrangement_priority": ["REMOTE", "HYBRID", "ONSITE"],
        },
        "standard_answers": {
            "years_of_experience": 3, "notice_period": "2 weeks", "willing_to_relocate": False,
            "requires_sponsorship_answer": "Yes, I will require H-1B sponsorship.",
            "veteran_status": "I am not a veteran", "disability_status": "I do not have a disability",
            "race_ethnicity": "Prefer not to say", "gender": "Prefer not to say",
        },
    }
    base.update(overrides)
    return CandidateProfile.model_validate(base)


def _req(analysis, text):
    return [r for r in analysis.requirements if r.text.lower() == text.lower()]


# --------------------------------------------------------------------------
# Role-aware selection
# --------------------------------------------------------------------------

def _payments_profile() -> CandidateProfile:
    return _profile(
        employment=[{
            "company": "Ledger Bank Corp", "title": "Software Engineer",
            "start_date": "2022-01", "end_date": "Present", "location": "Remote",
            "verified_bullets": [
                "Built Python services supporting payment settlement and reconciliation workflows.",
                "Designed PostgreSQL schemas for transaction ledgers processing 500K events/day.",
            ],
            "skills_used": ["python", "postgresql", "sql"],
        }],
        skills=["python", "postgresql", "sql", "git"],
    )


def test_payments_role_classified_and_boosts_backend_database():
    jd = "Required: Python, PostgreSQL. Join our Payments platform team."
    a = analyze_jd("Software Engineer, Payments", jd)
    profile = _payments_profile()
    graph = build_evidence_graph(profile)
    matches = match_requirements(a.requirements, graph, profile)
    role = classify_role("Software Engineer, Payments", a, graph)
    assert role.archetype == RoleArchetype.PAYMENTS
    assert role.category_boosts[RequirementCategory.BACKEND] > 1.0

    resume = generate_optimized_resume_content(profile, "Software Engineer, Payments", jd, a, matches, graph)
    assert check_resume_claims(resume, profile) == []


def _backend_profile() -> CandidateProfile:
    return _profile(
        employment=[{
            "company": "Widget Software Inc", "title": "Backend Software Engineer",
            "start_date": "2022-06", "end_date": "Present", "location": "Remote",
            "verified_bullets": [
                "Built and maintained REST APIs in Python using FastAPI serving 2M requests/day.",
                "Designed PostgreSQL schema migrations for a multi-tenant billing system.",
            ],
            "skills_used": ["python", "fastapi", "rest api", "postgresql"],
        }],
        skills=["python", "fastapi", "rest api", "postgresql", "docker", "git"],
    )


def test_backend_role_is_default_archetype_for_backend_title():
    jd = "Required: Python, FastAPI, PostgreSQL."
    a = analyze_jd("Backend Software Engineer", jd)
    profile = _backend_profile()
    graph = build_evidence_graph(profile)
    role = classify_role("Backend Software Engineer", a, graph)
    assert role.archetype == RoleArchetype.BACKEND


def _platform_profile() -> CandidateProfile:
    return _profile(
        employment=[{
            "company": "Cloudworks Inc", "title": "Platform Engineer",
            "start_date": "2021-01", "end_date": "Present", "location": "Remote",
            "verified_bullets": [
                "Provisioned AWS infrastructure with Terraform for internal developer platforms.",
                "Operated Kubernetes clusters running CI/CD pipelines for 40+ services.",
            ],
            "skills_used": ["aws", "terraform", "kubernetes", "ci/cd", "docker"],
        }],
        skills=["aws", "terraform", "kubernetes", "docker", "ci/cd", "git"],
    )


def test_platform_role_classified_from_title():
    jd = "Required: AWS, Terraform, Kubernetes, CI/CD."
    a = analyze_jd("Platform Engineer", jd)
    profile = _platform_profile()
    graph = build_evidence_graph(profile)
    role = classify_role("Platform Engineer", a, graph)
    assert role.archetype == RoleArchetype.PLATFORM
    assert role.category_boosts[RequirementCategory.DEVOPS] > 1.0


def _fullstack_profile() -> CandidateProfile:
    return _profile(
        employment=[{
            "company": "Appworks Ltd", "title": "Full Stack Software Engineer",
            "start_date": "2021-03", "end_date": "Present", "location": "Remote",
            "verified_bullets": [
                "Built React dashboards consuming Python/FastAPI backend services.",
                "Implemented REST APIs in Python for a customer-facing web application.",
            ],
            "skills_used": ["react", "python", "fastapi", "rest api"],
        }],
        skills=["react", "python", "fastapi", "rest api", "css", "git"],
    )


def test_fullstack_role_classified_when_frontend_and_backend_both_required():
    jd = "Required: React, Python, REST APIs."
    a = analyze_jd("Software Engineer", jd)
    profile = _fullstack_profile()
    graph = build_evidence_graph(profile)
    role = classify_role("Software Engineer", a, graph)
    assert role.archetype == RoleArchetype.FULL_STACK


def _qa_profile() -> CandidateProfile:
    return _profile(
        employment=[{
            "company": "QualityWorks Inc", "title": "QA Automation Engineer",
            "start_date": "2021-06", "end_date": "Present", "location": "Remote",
            "verified_bullets": [
                "Wrote Selenium and pytest automated test suites for a checkout flow.",
                "Built regression testing pipelines integrated with CI/CD.",
            ],
            "skills_used": ["selenium", "pytest", "unit testing", "ci/cd"],
        }],
        skills=["selenium", "pytest", "unit testing", "test automation", "ci/cd", "git"],
    )


def test_qa_sdet_role_classified_from_testing_heavy_jd():
    jd = "Required: Selenium, pytest, regression testing, test automation."
    a = analyze_jd("SDET", jd)
    profile = _qa_profile()
    graph = build_evidence_graph(profile)
    matches = match_requirements(a.requirements, graph, profile)
    role = classify_role("SDET", a, graph)
    assert role.archetype == RoleArchetype.QA_SDET
    selenium_match = [m for m in matches if m.requirement.normalized_value == "selenium"][0]
    assert selenium_match.status == MatchStatus.MATCHED


def _infrastructure_profile() -> CandidateProfile:
    return _profile(
        employment=[{
            "company": "ReliableOps Inc", "title": "Infrastructure Engineer",
            "start_date": "2021-01", "end_date": "Present", "location": "Remote",
            "verified_bullets": [
                "Operated production infrastructure on AWS with Terraform-managed IaC.",
                "Built Grafana/Prometheus dashboards for service-level monitoring.",
            ],
            "skills_used": ["aws", "terraform", "grafana", "prometheus", "docker"],
        }],
        skills=["aws", "terraform", "grafana", "prometheus", "docker", "git"],
    )


def test_infrastructure_role_classified_from_title():
    jd = "Required: AWS, Terraform, monitoring."
    a = analyze_jd("Infrastructure Engineer", jd)
    profile = _infrastructure_profile()
    graph = build_evidence_graph(profile)
    role = classify_role("Infrastructure Engineer", a, graph)
    assert role.archetype == RoleArchetype.INFRASTRUCTURE
    assert role.category_boosts[RequirementCategory.OBSERVABILITY] > 1.0


def _cloud_profile() -> CandidateProfile:
    return _profile(
        employment=[{
            "company": "SkyScale Inc", "title": "Cloud Software Engineer",
            "start_date": "2021-01", "end_date": "Present", "location": "Remote",
            "verified_bullets": [
                "Built serverless services on AWS Lambda and deployed via CloudFormation.",
                "Migrated workloads across AWS and GCP for a multi-cloud rollout.",
            ],
            "skills_used": ["aws", "lambda", "cloudformation", "gcp", "docker"],
        }],
        skills=["aws", "lambda", "cloudformation", "gcp", "docker", "git"],
    )


def test_cloud_role_classified_from_title():
    jd = "Required: AWS, GCP, Lambda."
    a = analyze_jd("Cloud Software Engineer", jd)
    profile = _cloud_profile()
    graph = build_evidence_graph(profile)
    role = classify_role("Cloud Software Engineer", a, graph)
    assert role.archetype == RoleArchetype.CLOUD


def _ai_backend_profile() -> CandidateProfile:
    return _profile(
        employment=[{
            "company": "Modelworks Inc", "title": "Software Engineer",
            "start_date": "2021-01", "end_date": "Present", "location": "Remote",
            "verified_bullets": [
                "Built Python backend services serving machine learning model predictions.",
                "Trained and deployed ML models for a recommendation pipeline.",
            ],
            "skills_used": ["python", "machine learning", "rest api"],
        }],
        skills=["python", "machine learning", "rest api", "git"],
    )


def test_ai_backend_role_requires_both_jd_signal_and_verified_evidence():
    jd = "Required: Python. Experience with machine learning model deployment."
    a = analyze_jd("Machine Learning Backend Engineer", jd)
    profile = _ai_backend_profile()  # has genuine verified "machine learning" evidence
    graph = build_evidence_graph(profile)
    role = classify_role("Machine Learning Backend Engineer", a, graph)
    assert role.archetype == RoleArchetype.AI_BACKEND


def test_ai_backend_role_never_assigned_without_verified_ml_evidence():
    """'AI/backend when evidence supports it' -- a JD asking for ML must
    NEVER classify AI_BACKEND for a candidate with no verified ML evidence
    (that would imply an unearned specialization emphasis)."""
    jd = "Required: Python. Experience with machine learning model deployment."
    a = analyze_jd("Machine Learning Backend Engineer", jd)
    profile = _backend_profile()  # no ML evidence at all
    graph = build_evidence_graph(profile)
    role = classify_role("Machine Learning Backend Engineer", a, graph)
    assert role.archetype != RoleArchetype.AI_BACKEND


# --------------------------------------------------------------------------
# Alternative requirements (OR form, distinct from the existing slash-form
# coverage in test_resume_optimizer_airbnb_regression.py)
# --------------------------------------------------------------------------

def test_or_form_alternative_language_requirement_single_item():
    a = analyze_jd("Backend Engineer", "Required: proficiency in Java or Kotlin or Python.")
    assert not _req(a, "java")
    assert not _req(a, "kotlin")
    assert not _req(a, "python")
    alt_reqs = [r for r in a.requirements if "java" in r.alternatives]
    assert len(alt_reqs) == 1
    assert set(alt_reqs[0].alternatives) == {"java", "kotlin", "python"}


def test_or_form_with_comma_list_before_or():
    a = analyze_jd("Backend Engineer", "Required: Java, Kotlin, or Python experience.")
    alt_reqs = [r for r in a.requirements if "java" in r.alternatives]
    assert len(alt_reqs) == 1
    assert set(alt_reqs[0].alternatives) == {"java", "kotlin", "python"}


def test_or_form_alternative_satisfied_by_verified_python():
    a = analyze_jd("Backend Engineer", "Required: Java or Kotlin or Python.")
    profile = _backend_profile()
    graph = build_evidence_graph(profile)
    matches = match_requirements(a.requirements, graph, profile)
    alt_match = [m for m in matches if m.requirement.alternatives][0]
    assert alt_match.status == MatchStatus.MATCHED
    assert alt_match.evidence_ids == ["skill:python"]


def test_unrelated_or_prose_never_becomes_fabricated_alternative_group():
    """'or' in ordinary prose (no >=2 recognized SKILL_VOCAB tokens) must
    never be force-grouped into a fabricated alternative requirement."""
    a = analyze_jd("Backend Engineer", "You will review code or documentation as needed.")
    assert not any(r.alternatives for r in a.requirements)


# --------------------------------------------------------------------------
# Unsupported technology: Go, K6, Locust
# --------------------------------------------------------------------------

def test_unsupported_go_stays_missing_never_matched():
    a = analyze_jd("Backend Engineer", "Required: Go.")
    profile = _backend_profile()
    graph = build_evidence_graph(profile)
    matches = match_requirements(a.requirements, graph, profile)
    assert matches[0].status == MatchStatus.MISSING
    assert "go" not in graph.skills


def test_unsupported_k6_and_locust_never_matched_and_never_on_resume():
    a = analyze_jd("Backend Engineer", "Nice to have: K6 or Locust for load testing.")
    profile = _qa_profile()  # has pytest/selenium testing evidence, but never K6/Locust
    graph = build_evidence_graph(profile)
    matches = match_requirements(a.requirements, graph, profile)

    k6_matches = [m for m in matches if m.requirement.normalized_value in ("k6", "locust")] or [
        m for m in matches if "k6" in m.requirement.alternatives or "locust" in m.requirement.alternatives
    ]
    assert k6_matches
    for m in k6_matches:
        assert m.status != MatchStatus.MATCHED
        if m.status == MatchStatus.TRANSFERABLE:
            # Transferable framing is allowed to name the missing tech, but
            # must always explicitly disclaim hands-on use of it.
            assert "never claimed as hands-on" in m.explanation.lower()

    resume = generate_optimized_resume_content(profile, "Backend Engineer", "Nice to have: K6 or Locust.", a, matches, graph)
    resume_text = " ".join(resume.skills_ordered).lower()
    assert "k6" not in resume_text
    assert "locust" not in resume_text


# --------------------------------------------------------------------------
# Years: 7-year requirement
# --------------------------------------------------------------------------

def test_seven_year_hyphenated_requirement_extracted():
    a = analyze_jd("Senior Backend Engineer", "Minimum 7-year software engineering experience required.")
    assert a.required_years == 7.0


def test_seven_year_requirement_produces_partial_gap_never_altered():
    a = analyze_jd("Senior Backend Engineer", "Minimum 7-year software engineering experience required.")
    profile = _backend_profile()
    graph = build_evidence_graph(profile)
    matches = match_requirements(a.requirements, graph, profile)
    years_match = [m for m in matches if m.requirement.category == RequirementCategory.YEARS_EXPERIENCE][0]
    assert years_match.status == MatchStatus.PARTIAL
    assert profile.standard_answers.years_of_experience == 3


# --------------------------------------------------------------------------
# Education: BA + "or equivalent experience"
# --------------------------------------------------------------------------

def test_ba_degree_pattern_recognized():
    a = analyze_jd("Engineer", "BA in Computer Science or a related field required.")
    assert "Bachelor's degree" in a.education_requirements


def test_or_equivalent_experience_marks_education_conditional():
    a = analyze_jd("Engineer", "Bachelor's degree required, or equivalent practical experience.")
    edu = [r for r in a.requirements if r.category == RequirementCategory.EDUCATION]
    assert edu and edu[0].conditional is True


def test_education_matched_against_verified_bs():
    a = analyze_jd("Engineer", "Bachelor's degree required, or equivalent practical experience.")
    profile = _backend_profile()
    graph = build_evidence_graph(profile)
    matches = match_requirements(a.requirements, graph, profile)
    edu_match = [m for m in matches if m.requirement.category == RequirementCategory.EDUCATION][0]
    assert edu_match.status == MatchStatus.MATCHED


# --------------------------------------------------------------------------
# Observability terminology expansion
# --------------------------------------------------------------------------

def test_observability_vocabulary_expanded():
    a = analyze_jd("Site Reliability Engineer", "Required: Datadog, CloudWatch, distributed tracing experience.")
    obs = {r.normalized_value for r in a.requirements if r.category == RequirementCategory.OBSERVABILITY}
    assert {"datadog", "cloudwatch", "distributed tracing"} <= obs


def test_observability_stays_missing_not_fabricated_for_docker_only_candidate():
    a = analyze_jd("Site Reliability Engineer", "Required: Datadog monitoring experience.")
    profile = _platform_profile()  # has Docker/Terraform/K8s but no Datadog/observability evidence
    graph = build_evidence_graph(profile)
    matches = match_requirements(a.requirements, graph, profile)
    datadog_match = [m for m in matches if m.requirement.normalized_value == "datadog"][0]
    assert datadog_match.status == MatchStatus.MISSING


# --------------------------------------------------------------------------
# Testing terminology expansion
# --------------------------------------------------------------------------

def test_testing_vocabulary_expanded():
    a = analyze_jd("SDET", "Required: Selenium, Cypress, regression testing, test coverage.")
    testing = {r.normalized_value for r in a.requirements if r.category == RequirementCategory.TESTING}
    assert {"selenium", "cypress", "regression testing", "test coverage"} <= testing


def test_selenium_matched_for_qa_candidate():
    a = analyze_jd("SDET", "Required: Selenium.")
    profile = _qa_profile()
    graph = build_evidence_graph(profile)
    matches = match_requirements(a.requirements, graph, profile)
    assert matches[0].status == MatchStatus.MATCHED


# --------------------------------------------------------------------------
# Fraud/ML nice-to-have
# --------------------------------------------------------------------------

def test_fraud_domain_and_ml_preferred_never_fabricated():
    a = analyze_jd(
        "Software Engineer, Payments", "Required: Python. Preferred: familiarity with fraud detection and machine learning models."
    )
    assert "fraud" in a.domain_signals
    ml = _req(a, "machine learning")
    assert ml and ml[0].priority == RequirementPriority.PREFERRED

    profile = _payments_profile()  # no ML evidence
    graph = build_evidence_graph(profile)
    matches = match_requirements(a.requirements, graph, profile)
    ml_match = [m for m in matches if m.requirement.text == "machine learning"][0]
    assert ml_match.status != MatchStatus.MATCHED

    resume = generate_optimized_resume_content(profile, "Software Engineer, Payments", "irrelevant", a, matches, graph)
    assert "machine learning" not in [s.lower() for s in resume.skills_ordered]
    assert check_resume_claims(resume, profile) == []


# --------------------------------------------------------------------------
# Semantic requirement deduplication
# --------------------------------------------------------------------------

def test_rest_synonyms_deduplicated_within_skill_category():
    jd = "You will build REST APIs. A solid understanding of RESTful services is required."
    a = analyze_jd("Backend Engineer", jd)
    backend_rest_items = [
        r for r in a.requirements
        if r.category == RequirementCategory.BACKEND and r.normalized_value in ("rest apis", "rest api", "restful", "rest")
    ]
    assert len(backend_rest_items) == 1
    # The RESPONSIBILITY-category "rest apis" signal is a separate,
    # independently-meaningful signal and must NOT be swallowed by the
    # SKILL_CATEGORIES dedup above.
    assert "rest apis" in a.responsibilities


def test_kubernetes_k8s_synonyms_deduplicated():
    a = analyze_jd("Platform Engineer", "Required: Kubernetes experience. Familiarity with K8s a plus.")
    k8s_items = [r for r in a.requirements if r.normalized_value in ("kubernetes", "k8s")]
    assert len(k8s_items) == 1
    # REQUIRED wins over the duplicate's PREFERRED priority.
    assert k8s_items[0].priority == RequirementPriority.REQUIRED


def test_monitoring_and_observability_stay_independently_tracked():
    """Regression guard: the Airbnb-JD suite depends on these two staying
    separate requirement items -- semantic dedup must never merge them."""
    a = analyze_jd("Engineer", "Experience with monitoring and observability tools required.")
    texts = {r.text for r in a.requirements if r.category == RequirementCategory.OBSERVABILITY}
    assert {"monitoring", "observability"} <= texts


# --------------------------------------------------------------------------
# Compensation parsing
# --------------------------------------------------------------------------

def test_compensation_range_parsed():
    a = analyze_jd("Engineer", "Compensation: $120,000 - $150,000 per year.")
    assert a.compensation_min == 120000.0
    assert a.compensation_max == 150000.0
    assert a.compensation_period == "year"
    assert a.compensation_currency == "USD"


def test_compensation_k_shorthand_parsed():
    a = analyze_jd("Engineer", "Salary range: $90k-$110k annually.")
    assert a.compensation_min == 90000.0
    assert a.compensation_max == 110000.0
    assert a.compensation_period == "year"


def test_compensation_ceiling_only_parsed():
    a = analyze_jd("Engineer", "Pay up to $95k depending on experience.")
    assert a.compensation_min is None
    assert a.compensation_max == 95000.0


def test_compensation_absent_stays_none_never_guessed():
    a = analyze_jd("Engineer", "Build backend services in Python.")
    assert a.compensation_min is None
    assert a.compensation_max is None
    assert a.compensation_period == ""
    assert a.compensation_currency == ""


def test_compensation_alignment_surfaces_in_quality_report(tmp_env):
    """Compensation parsing 'where appropriate' (CLAUDE.md): surfaced as a
    purely informational quality-report field, never a matching/eligibility
    blocker -- the job is still READY even when the JD range sits below the
    candidate's stated salary_min_usd preference."""
    from app.candidate.profile import save_profile
    from app.jobs_repo import insert_job
    from app.models import ApplicationMode, Job

    profile = _backend_profile()  # preferences.salary_min_usd == 110000
    save_profile(profile)
    job = Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote",
        description="Required: Python, FastAPI, PostgreSQL. Compensation: $80,000 - $95,000 per year.",
        mode=ApplicationMode.ASSIST,
    )
    job_id = insert_job(job)
    result = optimize_resume(job_id)
    assert result.status == "READY"
    comp = result.quality_report["compensation_alignment"]
    assert comp["jd_compensation_min"] == 80000.0
    assert comp["jd_compensation_max"] == 95000.0
    assert comp["label"] == "BELOW_PREFERENCE"


# --------------------------------------------------------------------------
# Target role / headline: a SEPARATE claim type from verified employment
# titles -- truthful, never echoes unverified JD title tokens, never
# compared against EmploymentEntry.title.
# --------------------------------------------------------------------------

def test_payments_evidence_allows_truthful_payments_target_role():
    """JD 'Software Engineer, Payments' + verified payments evidence ->
    a truthful payments target role is allowed (domain qualifier surfaces)."""
    jd = "Required: Python, PostgreSQL. Join our Payments platform team."
    a = analyze_jd("Software Engineer, Payments", jd)
    profile = _payments_profile()
    graph = build_evidence_graph(profile)
    role = classify_role("Software Engineer, Payments", a, graph)
    target_role = build_target_role("Software Engineer, Payments", a, graph, role)
    assert target_role == "Software Engineer, Payments"
    assert _validate_target_role(target_role, profile) == []


def test_java_engineer_jd_never_leaks_java_for_python_only_candidate():
    a = analyze_jd("Java Backend Engineer", "Required: Java, Spring Boot.")
    profile = _backend_profile()  # verified Python only, no Java
    graph = build_evidence_graph(profile)
    role = classify_role("Java Backend Engineer", a, graph)
    target_role = build_target_role("Java Backend Engineer", a, graph, role)
    assert "java" not in target_role.lower()
    assert target_role == "Backend Software Engineer"
    assert _validate_target_role(target_role, profile) == []


def test_staff_backend_engineer_jd_never_leaks_staff_for_non_staff_candidate():
    a = analyze_jd("Staff Backend Engineer", "Required: Python.")
    profile = _backend_profile()  # no verified Staff-level title
    graph = build_evidence_graph(profile)
    role = classify_role("Staff Backend Engineer", a, graph)
    target_role = build_target_role("Staff Backend Engineer", a, graph, role)
    assert "staff" not in target_role.lower()
    assert _validate_target_role(target_role, profile) == []


def test_target_role_never_leaks_unverified_jd_technology_end_to_end(tmp_env):
    from app.candidate.profile import save_profile
    from app.jobs_repo import insert_job
    from app.models import ApplicationMode, Job

    profile = _backend_profile()
    save_profile(profile)
    job = Job(
        title="Java Backend Engineer", company="Acme Corp", location="Remote",
        description="Required: Java, Spring Boot, Kafka, Kubernetes, 7+ years experience. PhD in Computer Science required.",
        mode=ApplicationMode.ASSIST,
    )
    job_id = insert_job(job)
    result = optimize_resume(job_id)
    assert result.status == "READY"

    from pathlib import Path
    from app.resume_optimizer import repo as ro_repo

    variant = ro_repo.get_current_variant(job_id)
    resume_text = Path(variant["resume_txt_path"]).read_text().lower()
    for fabricated in ("java", "spring boot", "kafka", "kubernetes", "phd", "staff"):
        assert fabricated not in resume_text, f"'{fabricated}' must never be fabricated onto a mismatched resume"


def test_historical_employment_titles_remain_byte_for_byte_unchanged():
    """The target role is a wholly separate string -- it must never rewrite
    or alter any verified employment-history title."""
    a = analyze_jd("Java Backend Engineer", "Required: Java.")
    profile = _backend_profile()
    graph = build_evidence_graph(profile)
    matches = match_requirements(a.requirements, graph, profile)
    resume = generate_optimized_resume_content(profile, "Java Backend Engineer", "Required: Java.", a, matches, graph)
    assert [e.title for e in resume.experience] == [e.title for e in profile.employment]
    assert resume.experience[0].title == "Backend Software Engineer"  # the candidate's real, unaltered title


def test_target_role_deterministic_and_idempotent():
    a = analyze_jd("Platform Engineer", "Required: AWS, Terraform, Kubernetes.")
    profile = _platform_profile()
    graph = build_evidence_graph(profile)
    role = classify_role("Platform Engineer", a, graph)
    r1 = build_target_role("Platform Engineer", a, graph, role)
    r2 = build_target_role("Platform Engineer", a, graph, role)
    assert r1 == r2 == "Platform Engineer"


def test_claim_checker_passes_truthful_normalized_target_role():
    profile = _platform_profile()
    assert _validate_target_role("Platform Engineer", profile) == []
    assert _validate_target_role("Python Backend Software Engineer", _backend_profile()) == []


def test_claim_checker_rejects_unsupported_target_role_technology():
    violations = _validate_target_role("Java Backend Software Engineer", _backend_profile())
    assert any("java" in v.lower() for v in violations)


def test_claim_checker_rejects_unsupported_target_role_seniority():
    violations = _validate_target_role("Staff Backend Software Engineer", _backend_profile())
    assert any("staff" in v.lower() and "seniority" in v.lower() for v in violations)


def test_claim_checker_rejects_unsupported_target_role_family():
    violations = _validate_target_role("Chief Executive Officer", _backend_profile())
    assert any("family" in v.lower() for v in violations)


def test_claim_checker_rejects_unsupported_target_role_domain():
    violations = _validate_target_role("Software Engineer, Payments", _backend_profile())  # no payments evidence
    assert any("domain" in v.lower() for v in violations)


def test_claim_checker_accepts_empty_target_role():
    assert _validate_target_role("", _backend_profile()) == []


# --------------------------------------------------------------------------
# select_bullets: non-redundant greedy coverage
# --------------------------------------------------------------------------

def test_select_bullets_prefers_novel_coverage_over_redundant_duplicate():
    """A second bullet that only restates a term the top bullet already
    covers must lose out to a bullet with a comparable raw score that covers
    a genuinely different, still-relevant requirement term -- a plain top-K-
    by-score selection would have kept both Python bullets and dropped
    Docker; the greedy marginal-coverage selection must not."""
    model = RelevanceModel(weights={"python": 3.0, "docker": 2.8})
    bullets = [
        "Built Python services for production APIs.",
        "Wrote Python scripts for internal automation tooling.",  # fully redundant with bullet 0
        "Used Docker for containerized deployments.",  # diverse coverage, comparable raw score
    ]
    selected = select_bullets(bullets, model, cap=2)
    assert selected == [bullets[0], bullets[2]]
    assert bullets[1] not in selected


def test_select_bullets_respects_cap_and_falls_back_when_empty():
    assert select_bullets([], RelevanceModel(weights={}), cap=3) == []
    only = ["A single bullet with no scored terms."]
    assert select_bullets(only, RelevanceModel(weights={}), cap=3) == only


def test_select_bullets_never_selects_more_than_available():
    model = RelevanceModel(weights={"python": 1.0})
    bullets = ["Used Python.", "Used Python again."]
    assert len(select_bullets(bullets, model, cap=5)) == 2


# --------------------------------------------------------------------------
# Determinism / idempotency
# --------------------------------------------------------------------------

def test_generate_optimized_resume_content_is_deterministic():
    jd = "Required: Python, PostgreSQL, Docker."
    a = analyze_jd("Backend Software Engineer", jd)
    profile = _backend_profile()
    graph = build_evidence_graph(profile)
    matches = match_requirements(a.requirements, graph, profile)

    r1 = generate_optimized_resume_content(profile, "Backend Software Engineer", jd, a, matches, graph)
    r2 = generate_optimized_resume_content(profile, "Backend Software Engineer", jd, a, matches, graph)
    assert r1.skills_ordered == r2.skills_ordered
    assert [e.bullets for e in r1.experience] == [e.bullets for e in r2.experience]
    assert r1.target_role == r2.target_role


def test_optimizer_version_bump_forces_regeneration_identity_change(tmp_env):
    """CLAUDE.md idempotency contract: a changed optimizer_version is part
    of the resume_variants identity key, so a content-selection rewrite
    never silently reuses a pre-change variant row for the same job/JD/
    profile. Bumped to v3 for the select_bullets() one-page-overflow fix
    (canary-candidate investigation): the v3-JD-intelligence version (v2)
    always padded an entry's bullets/projects out to their cap even once
    genuinely relevant content was exhausted, which systematically inflated
    every generated resume and silently exhausted one_page.enforce_one_page's
    bounded compression ladder before reaching one page for nearly all real
    JDs. v3 stops selecting once no remaining bullet adds relevance/novelty,
    once at least one relevant bullet is already chosen."""
    from app.resume_optimizer.fingerprint import OPTIMIZER_VERSION

    assert OPTIMIZER_VERSION == "resume-optimizer-v3"
