"""CLAUDE.md Phase 14 section 76: JD extraction acceptance -- required vs
preferred, negation, years, education, certification, tools, responsibilities,
domain, sponsorship language, employment type."""

from app.resume_optimizer.jd_analysis import analyze_jd
from app.resume_optimizer.models import RequirementCategory, RequirementPriority


def _req(analysis, text):
    return [r for r in analysis.requirements if r.text.lower() == text.lower()]


def test_required_vs_preferred_sections():
    jd = (
        "Required: Python, PostgreSQL, Docker. "
        "Preferred: Kubernetes, GraphQL."
    )
    a = analyze_jd("Backend Engineer", jd)
    python_reqs = _req(a, "python")
    assert python_reqs and python_reqs[0].priority == RequirementPriority.REQUIRED
    k8s_reqs = _req(a, "kubernetes")
    assert k8s_reqs and k8s_reqs[0].priority == RequirementPriority.PREFERRED


def test_local_phrase_overrides_stale_section_header():
    jd = "Preferred: Kubernetes. Bachelor's degree in Computer Science required."
    a = analyze_jd("Backend Engineer", jd)
    edu = [r for r in a.requirements if r.category == RequirementCategory.EDUCATION]
    assert edu and edu[0].priority == RequirementPriority.REQUIRED


def test_negation_excludes_requirement():
    jd = "Experience with Java is not required. Python is required."
    a = analyze_jd("Backend Engineer", jd)
    assert not _req(a, "java")
    assert _req(a, "python")


def test_negation_variants():
    for phrase in [
        "Prior AWS certification is not necessary.",
        "AWS certification is not mandatory.",
    ]:
        a = analyze_jd("Engineer", phrase)
        assert a.certification_requirements == [], f"failed for: {phrase}"


def test_conditional_language_marked_but_not_dropped():
    jd = "AWS experience may be considered depending on other qualifications."
    a = analyze_jd("Engineer", jd)
    aws = _req(a, "aws")
    assert aws and aws[0].conditional is True


def test_years_extraction():
    a = analyze_jd("Engineer", "This role requires 5+ years of experience.")
    assert a.required_years == 5.0


def test_years_range_extraction():
    a = analyze_jd("Engineer", "3-5 years of experience required.")
    assert a.required_years == 3.0


def test_years_negation_not_extracted():
    a = analyze_jd("Engineer", "5+ years experience is not required for junior applicants.")
    assert a.required_years is None


def test_education_extraction():
    a = analyze_jd("Engineer", "Master's degree required. PhD preferred.")
    assert "Master's degree" in a.education_requirements
    assert "PhD" in a.education_requirements


def test_certification_extraction_bounded_span():
    a = analyze_jd("Engineer", "AWS Certified Developer is a plus for this role.")
    assert a.certification_requirements == ["AWS Certified Developer"]


def test_certification_dedup_broader_match_wins():
    a = analyze_jd("Engineer", "AWS Certified Solutions Architect required.")
    assert a.certification_requirements == ["AWS Certified Solutions Architect"]


def test_tools_extraction():
    a = analyze_jd("Engineer", "Experience with Git, Docker, and Kubernetes required.")
    tools = {r.normalized_value for r in a.requirements if r.category == RequirementCategory.TOOL}
    assert "git" in tools


def test_responsibilities_extraction():
    a = analyze_jd("Engineer", "You will design REST APIs and own CI/CD pipelines and debugging.")
    assert "rest apis" in a.responsibilities
    assert "ci/cd" in a.responsibilities
    assert "debugging" in a.responsibilities


def test_domain_signal_extraction():
    a = analyze_jd("Engineer", "Join our payments and fintech platform team.")
    assert "payments" in a.domain_signals
    assert "fintech" in a.domain_signals


def test_domain_not_specified_when_absent():
    a = analyze_jd("Engineer", "Build backend services in Python.")
    assert a.domain_signals == []


def test_sponsorship_language_detected():
    a = analyze_jd("Engineer", "We offer H-1B sponsorship for qualified candidates.")
    assert a.sponsorship_language_present is True


def test_sponsorship_language_absent():
    a = analyze_jd("Engineer", "Build backend services in Python.")
    assert a.sponsorship_language_present is False


def test_salary_mentioned():
    a = analyze_jd("Engineer", "Compensation: $120,000 - $150,000 per year.")
    assert a.salary_mentioned is True


def test_analyzer_is_pure_never_reads_candidate_data():
    """analyze_jd takes only title/description -- no profile argument exists
    at all, which is itself the structural guarantee CLAUDE.md section 3
    requires (JD analysis never reads candidate data)."""
    import inspect

    sig = inspect.signature(analyze_jd)
    assert list(sig.parameters) == ["job_title", "description"]
