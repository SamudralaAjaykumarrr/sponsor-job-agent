"""Post-release bug-fix regression tests (fix/jd-requirement-classification).

A real Airbnb Payments JD exposed several JD requirement parser / evidence
matcher classification bugs. This uses a SANITIZED, clearly-synthetic JD text
and a clearly-synthetic candidate profile -- never real Airbnb text and never
the developer's real candidate_data/profile.json -- to regression-test each
bug independently:

  1. alternative programming languages (Java/Kotlin/Python) must be ONE
     requirement satisfied by ANY verified alternative, never three separate
     mandatory requirements.
  2. "preferably" is a preference phrase, not a REQUIRED trigger.
  3. "React (or any equivalent JS library) would be nice to have" must be
     PREFERRED, not REQUIRED.
  4. a payments-domain JD must recognize payments-domain candidate evidence
     even when phrased with the singular "payment" rather than "payments".
  5. "testing" must be matched via genuinely equivalent verified evidence
     (unit testing / integration testing bullets), never declared MISSING
     when that evidence exists, and never fabricated.
  6. "monitoring"/"observability" must stay MISSING for a candidate whose
     only DEVOPS-adjacent evidence is Docker/CI-CD -- never inflated to
     TRANSFERABLE just because they used to share a DEVOPS category.
  7. "3+ years" extraction/matching still works.
  8. "Familiarity with machine learning is a plus" must be PREFERRED.
  9. "would be a huge plus, but not required" must be kept as PREFERRED, not
     silently dropped by the hard-negation ("not required") pattern.
"""

from app.candidate.schema import CandidateProfile
from app.resume_optimizer.evidence import build_evidence_graph
from app.resume_optimizer.jd_analysis import analyze_jd
from app.resume_optimizer.matching import match_requirements
from app.resume_optimizer.models import MatchStatus, RequirementCategory, RequirementPriority

AIRBNB_LIKE_JD_TITLE = "Software Engineer, Payments Platform"

AIRBNB_LIKE_JD = """
We are looking for a Software Engineer to join our Payments platform team,
building infrastructure that powers global payments processing.

Required Qualifications:
- Proficient in at least one major programming language (preferably Java/Kotlin/Python).
- Strong experience with SQL/PostgreSQL/MySQL.
- 3+ years of software engineering experience.
- Solid testing practices across the stack.

Preferred Qualifications:
- Experience in React (or any equivalent JS library) would be nice to have.
- Familiarity with machine learning is a plus.
- Experience with monitoring and observability tools would be a huge plus, but not required.
"""


def _payments_profile() -> CandidateProfile:
    """Sanitized, clearly-synthetic candidate profile (modeled on
    tests/conftest.py's `sample_profile` style) -- not real candidate data.
    Deliberately: verified Python (not Java/Kotlin), verified SQL/
    PostgreSQL, a payments-domain bullet using the SINGULAR "payment"
    phrasing (distinct from the JD's plural "payments"), verified unit/
    integration TESTING evidence phrased without the bare word "testing",
    verified Docker/CI-CD (DEVOPS) but deliberately NO monitoring/
    observability/Grafana/Prometheus evidence, and no React/ML/Kotlin/Java."""
    return CandidateProfile.model_validate({
        "contact": {
            "full_name": "Test Candidate", "email": "test.candidate@example.com",
            "phone": "555-000-2222", "city": "Austin", "state": "TX",
            "linkedin_url": "", "github_url": "", "portfolio_url": "",
        },
        "employment": [
            {
                "company": "Example Bank Corp", "title": "Software Engineer",
                "start_date": "2022-06", "end_date": "Present", "location": "Remote",
                "verified_bullets": [
                    "Built Python services supporting payment routing and settlement workflows.",
                    "Wrote unit tests and integration test suites for critical backend services.",
                    "Automated CI/CD pipelines using Docker containers for backend deployments.",
                ],
                "skills_used": [
                    "python", "postgresql", "sql", "unit testing", "integration testing", "docker", "ci/cd",
                ],
            }
        ],
        "skills": ["python", "sql", "postgresql", "docker", "ci/cd", "unit testing", "integration testing", "git"],
        "projects": [],
        "education": [
            {"school": "State University", "degree": "B.S.", "field_of_study": "Computer Science", "graduation_date": "2022-05"}
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
    })


def _req(analysis, text):
    return [r for r in analysis.requirements if r.text.lower() == text.lower()]


def test_alternative_programming_languages_single_requirement_satisfied_by_python():
    a = analyze_jd(AIRBNB_LIKE_JD_TITLE, AIRBNB_LIKE_JD)
    profile = _payments_profile()
    graph = build_evidence_graph(profile)
    matches = match_requirements(a.requirements, graph, profile)

    # Never three separate mandatory requirements.
    assert not _req(a, "java")
    assert not _req(a, "kotlin")
    assert not _req(a, "python")

    alt_reqs = [r for r in a.requirements if "java" in r.alternatives]
    assert len(alt_reqs) == 1
    lang_req = alt_reqs[0]
    assert set(lang_req.alternatives) == {"java", "kotlin", "python"}

    lang_match = [m for m in matches if m.requirement is lang_req][0]
    assert lang_match.status == MatchStatus.MATCHED
    assert lang_match.evidence_ids == ["skill:python"]


def test_sql_alternative_group_matched_via_postgresql_or_sql():
    a = analyze_jd(AIRBNB_LIKE_JD_TITLE, AIRBNB_LIKE_JD)
    profile = _payments_profile()
    graph = build_evidence_graph(profile)
    matches = match_requirements(a.requirements, graph, profile)

    db_reqs = [r for r in a.requirements if "postgresql" in r.alternatives]
    assert len(db_reqs) == 1
    db_match = [m for m in matches if m.requirement is db_reqs[0]][0]
    assert db_match.status == MatchStatus.MATCHED


def test_preferably_examples_never_become_separate_mandatory_requirements():
    """'(preferably Java/Kotlin/Python)' -- the parenthetical examples must
    never surface as their own separate REQUIRED items."""
    a = analyze_jd(AIRBNB_LIKE_JD_TITLE, AIRBNB_LIKE_JD)
    assert not _req(a, "java")
    assert not _req(a, "kotlin")


def test_react_nice_to_have_is_preferred_not_required():
    a = analyze_jd(AIRBNB_LIKE_JD_TITLE, AIRBNB_LIKE_JD)
    react = _req(a, "react")
    assert react and react[0].priority == RequirementPriority.PREFERRED


def test_payments_domain_recognized_via_singular_candidate_evidence():
    a = analyze_jd(AIRBNB_LIKE_JD_TITLE, AIRBNB_LIKE_JD)
    assert "payments" in a.domain_signals

    profile = _payments_profile()
    graph = build_evidence_graph(profile)
    # Candidate bullet uses the singular "payment routing ... workflows"
    # phrasing -- must still register as payments-domain evidence, not
    # silently missed by a plural-only literal match.
    assert "payments" in graph.domains


def test_testing_matched_via_unit_and_integration_testing_evidence_not_fabricated():
    a = analyze_jd(AIRBNB_LIKE_JD_TITLE, AIRBNB_LIKE_JD)
    profile = _payments_profile()
    graph = build_evidence_graph(profile)
    matches = match_requirements(a.requirements, graph, profile)

    testing_matches = [
        m for m in matches
        if m.requirement.category == RequirementCategory.RESPONSIBILITY and m.requirement.normalized_value == "testing"
    ]
    assert testing_matches
    m = testing_matches[0]
    assert m.status == MatchStatus.MATCHED
    # Backed by a genuinely DIRECT_VERIFIED equivalent skill (unit/
    # integration testing), never fabricated.
    assert m.evidence_ids == ["skill:unit testing"]
    assert graph.skills["unit testing"].level.value == "DIRECT_VERIFIED"


def test_observability_missing_not_fabricated_from_docker_and_cicd():
    a = analyze_jd(AIRBNB_LIKE_JD_TITLE, AIRBNB_LIKE_JD)
    profile = _payments_profile()
    graph = build_evidence_graph(profile)
    matches = match_requirements(a.requirements, graph, profile)

    obs_matches = [m for m in matches if m.requirement.text in ("monitoring", "observability")]
    assert len(obs_matches) == 2
    for m in obs_matches:
        # Verified Docker/CI-CD evidence must never be reframed as
        # observability/monitoring experience -- MISSING, never TRANSFERABLE.
        assert m.status == MatchStatus.MISSING


def test_three_plus_years_extracted_and_matched():
    a = analyze_jd(AIRBNB_LIKE_JD_TITLE, AIRBNB_LIKE_JD)
    assert a.required_years == 3.0

    profile = _payments_profile()
    graph = build_evidence_graph(profile)
    matches = match_requirements(a.requirements, graph, profile)
    years_match = [m for m in matches if m.requirement.category == RequirementCategory.YEARS_EXPERIENCE][0]
    assert years_match.status == MatchStatus.MATCHED


def test_machine_learning_familiarity_is_preferred_not_required():
    a = analyze_jd(AIRBNB_LIKE_JD_TITLE, AIRBNB_LIKE_JD)
    ml = _req(a, "machine learning")
    assert ml and ml[0].priority == RequirementPriority.PREFERRED


def test_huge_plus_but_not_required_kept_as_preferred_not_dropped():
    """'would be a huge plus, but not required' must classify as PREFERRED,
    never silently dropped by the hard-negation ('not required') pattern
    reserved for a genuinely inapplicable requirement."""
    a = analyze_jd(AIRBNB_LIKE_JD_TITLE, AIRBNB_LIKE_JD)
    monitoring = _req(a, "monitoring")
    observability = _req(a, "observability")
    assert monitoring and monitoring[0].priority == RequirementPriority.PREFERRED
    assert observability and observability[0].priority == RequirementPriority.PREFERRED
