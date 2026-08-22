"""Evidence graph + requirement matching (CLAUDE.md Phase 14 sections 6-11,
acceptance scenarios A-E)."""

from app.resume_optimizer.evidence import build_evidence_graph
from app.resume_optimizer.jd_analysis import analyze_jd
from app.resume_optimizer.matching import match_requirements
from app.resume_optimizer.models import EvidenceLevel, MatchStatus


def test_direct_verified_skill_matches(sample_profile):
    graph = build_evidence_graph(sample_profile)
    assert graph.skills["python"].level == EvidenceLevel.DIRECT_VERIFIED
    assert graph.skills["python"].supporting_bullets


def test_matching_scenario_a_supported_skills_matched(sample_profile):
    """Scenario A: JD asks Python/PostgreSQL/AWS, candidate has verified
    evidence -> MATCHED."""
    a = analyze_jd("Backend Engineer", "Required: Python, PostgreSQL, AWS.")
    graph = build_evidence_graph(sample_profile)
    matches = match_requirements(a.requirements, graph, sample_profile)
    by_text = {m.requirement.text: m for m in matches}
    assert by_text["python"].status == MatchStatus.MATCHED
    assert by_text["postgresql"].status == MatchStatus.MATCHED
    assert by_text["aws"].status in (MatchStatus.MATCHED, MatchStatus.PARTIAL)


def test_matching_scenario_b_unsupported_skill_missing_never_matched(sample_profile):
    """Scenario B: JD asks unsupported Go -> MISSING, never inserted/matched."""
    a = analyze_jd("Backend Engineer", "Required: Go.")
    graph = build_evidence_graph(sample_profile)
    matches = match_requirements(a.requirements, graph, sample_profile)
    assert matches[0].status == MatchStatus.MISSING
    assert "go" not in graph.skills


def test_matching_scenario_c_unsupported_certification_missing(sample_profile):
    a = analyze_jd("Backend Engineer", "AWS Certified Solutions Architect required.")
    graph = build_evidence_graph(sample_profile)
    matches = match_requirements(a.requirements, graph, sample_profile)
    cert_matches = [m for m in matches if m.requirement.category.value == "CERTIFICATION"]
    assert cert_matches and cert_matches[0].status == MatchStatus.MISSING


def test_matching_scenario_d_years_gap_shown_not_altered(sample_profile):
    a = analyze_jd("Backend Engineer", "7+ years of experience required.")
    graph = build_evidence_graph(sample_profile)
    matches = match_requirements(a.requirements, graph, sample_profile)
    years_match = [m for m in matches if m.requirement.category.value == "YEARS_EXPERIENCE"][0]
    assert years_match.status == MatchStatus.PARTIAL
    assert sample_profile.standard_answers.years_of_experience == 3  # never altered


def test_matching_scenario_e_transferable_never_claims_hands_on(sample_profile):
    """Scenario E: JD asks Java but candidate has verified backend/API
    experience in a different language -- transferable explanation only,
    never a fabricated Java hands-on claim."""
    a = analyze_jd("Backend Engineer", "Required: Java.")
    graph = build_evidence_graph(sample_profile)
    matches = match_requirements(a.requirements, graph, sample_profile)
    m = matches[0]
    # LANGUAGE is excluded from transferable-eligible categories (CLAUDE.md
    # section 8 / acceptance scenario B) -- Java, like Go, is always MISSING,
    # never a fabricated "transferable" hands-on claim.
    assert m.status == MatchStatus.MISSING
    assert "java" not in [b.lower() for e in sample_profile.employment for b in e.verified_bullets]


def test_transferable_backend_experience_shown_via_responsibility_not_fake_skill(sample_profile):
    """CLAUDE.md acceptance scenario E's intent -- 'transferable backend
    experience exists' -- is honestly represented via RESPONSIBILITY
    evidence (the candidate's real REST API/backend bullets), not by
    fabricating a skill-level Java match."""
    a = analyze_jd("Backend Engineer", "Required: Java. You will build REST APIs.")
    graph = build_evidence_graph(sample_profile)
    matches = match_requirements(a.requirements, graph, sample_profile)
    java_match = [m for m in matches if m.requirement.text == "java"][0]
    resp_match = [m for m in matches if m.requirement.category.value == "RESPONSIBILITY"][0]
    assert java_match.status == MatchStatus.MISSING
    assert resp_match.status == MatchStatus.MATCHED


def test_no_certifications_field_always_missing(sample_profile):
    """CandidateProfile has no verified certifications field at all --
    CLAUDE.md section 17: any JD certification requirement is always
    MISSING, never fabricated."""
    a = analyze_jd("Engineer", "PMP required.")
    graph = build_evidence_graph(sample_profile)
    matches = match_requirements(a.requirements, graph, sample_profile)
    assert matches[0].status == MatchStatus.MISSING


def test_education_matched_when_verified(sample_profile):
    a = analyze_jd("Engineer", "Bachelor's degree required.")
    graph = build_evidence_graph(sample_profile)
    matches = match_requirements(a.requirements, graph, sample_profile)
    assert matches[0].status == MatchStatus.MATCHED


def test_education_missing_when_higher_than_verified(sample_profile):
    a = analyze_jd("Engineer", "PhD required.")
    graph = build_evidence_graph(sample_profile)
    matches = match_requirements(a.requirements, graph, sample_profile)
    assert matches[0].status == MatchStatus.MISSING


def test_responsibility_matched_from_verified_bullet(sample_profile):
    a = analyze_jd("Engineer", "You will build REST APIs.")
    graph = build_evidence_graph(sample_profile)
    matches = match_requirements(a.requirements, graph, sample_profile)
    resp = [m for m in matches if m.requirement.category.value == "RESPONSIBILITY"][0]
    assert resp.status == MatchStatus.MATCHED
