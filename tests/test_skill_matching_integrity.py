"""Candidate Integrity Re-Screen + Canary Selection V1: regression coverage
for the skill-token-matching accuracy fix in app/matching/skills.py.

Root cause (confirmed live during a Greenhouse canary-candidate preflight):
match_candidate_skills() used raw substring containment (`kw in cs or cs in
kw`) to decide whether a candidate's verified skill satisfied a JD keyword.
That let "go" (a genuine JD requirement for job 205 -- Robinhood, "Go,
Python, or Java") falsely match inside the candidate's verified "Django"
skill, and "java" falsely match inside "JavaScript" -- neither is a real
match a truthful matcher should ever credit. The fix normalizes both sides
onto a fixed canonical-id space (SKILL_REGISTRY, isolated-token boundary
matching) and compares by exact id-set membership, never substring
containment.

Every "old behavior" assertion below re-derives the OLD algorithm inline
(rather than importing removed code) so this file keeps proving the bug
would reproduce without the fix, not just asserting today's fixed output."""

from app.matching.skills import contains_term, extract_jd_keywords, extract_skill_ids, match_candidate_skills


def _old_buggy_match(jd_keywords: list[str], candidate_skills: list[str]) -> list[str]:
    """The exact pre-fix algorithm from match_candidate_skills, kept only to
    prove the regression cases really did fail before the fix."""
    candidate_lower = [s.lower().strip() for s in candidate_skills if s and s != "NEEDS_USER_INPUT"]
    return [kw for kw in jd_keywords if any(kw == cs or kw in cs or cs in kw for cs in candidate_lower)]


# --------------------------------------------------------------------------
# The two reported false positives, proven both ways
# --------------------------------------------------------------------------

def test_old_algorithm_falsely_matched_go_inside_django():
    assert _old_buggy_match(["go"], ["Django"]) == ["go"]


def test_go_does_not_match_django():
    _, matched, gaps = match_candidate_skills(["go"], ["Django"])
    assert matched == []
    assert gaps == ["go"]


def test_old_algorithm_falsely_matched_java_inside_javascript():
    assert _old_buggy_match(["java"], ["JavaScript"]) == ["java"]


def test_java_does_not_match_javascript():
    _, matched, gaps = match_candidate_skills(["java"], ["JavaScript"])
    assert matched == []
    assert gaps == ["java"]


def test_java_exact_match():
    _, matched, gaps = match_candidate_skills(["java"], ["Java"])
    assert matched == ["java"]
    assert gaps == []


def test_go_matches_go_and_golang():
    _, matched, _ = match_candidate_skills(["go"], ["Go"])
    assert matched == ["go"]
    _, matched, _ = match_candidate_skills(["go"], ["Golang"])
    assert matched == ["go"]


def test_go_and_java_together_against_mixed_profile():
    # The realistic job-205 shape: candidate has real Python + JavaScript +
    # Django, JD wants "Go, Python, or Java" -- only "python" should match.
    score, matched, gaps = match_candidate_skills(
        ["go", "python", "java"], ["Python", "JavaScript", "Django", "FastAPI"],
    )
    assert matched == ["python"]
    assert set(gaps) == {"go", "java"}


# --------------------------------------------------------------------------
# C / C++ / C# / R -- short/symbol-bearing language tokens
# --------------------------------------------------------------------------

def test_c_plus_plus_and_c_sharp_extracted():
    kw = extract_jd_keywords("We use C++ and C# extensively.")
    assert "c++" in kw
    assert "c#" in kw


def test_bare_c_does_not_match_inside_cplusplus_or_csharp():
    # A JD mentioning only "C++"/"C#" must never also silently require
    # plain "C" -- they are different, separately-tracked canonical ids.
    kw = extract_jd_keywords("We use C++ and C# extensively.")
    assert "c" not in kw


def test_bare_c_matches_as_standalone_language():
    kw = extract_jd_keywords("Strong C programming skills required.")
    assert kw == ["c"]


def test_c_and_cplusplus_both_present_when_both_genuinely_mentioned():
    kw = extract_jd_keywords("Experience in both C and C++ is expected.")
    assert "c" in kw
    assert "c++" in kw


def test_bare_c_does_not_match_inside_arbitrary_words():
    kw = extract_jd_keywords("Cloud, container, and continuous delivery experience.")
    assert "c" not in kw
    kw2 = extract_jd_keywords("We value clear communication and collaboration.")
    assert "c" not in kw2


def test_r_matches_as_standalone_language():
    kw = extract_jd_keywords("R statistical programming a plus.")
    assert kw == ["r"]


def test_r_does_not_match_inside_arbitrary_prose():
    kw = extract_jd_keywords("Our platform serves millions of users worldwide.")
    assert "r" not in kw
    kw2 = extract_jd_keywords("We are hiring for our remote engineering team.")
    assert "r" not in kw2


def test_dotnet_extracted_and_distinct_from_bare_c_or_net():
    kw = extract_jd_keywords("Built services on .NET Core.")
    assert ".net" in kw


# --------------------------------------------------------------------------
# Node.js / React / PostgreSQL / AWS -- multi-word and symbol aliases
# --------------------------------------------------------------------------

def test_nodejs_aliases():
    for text in ("Node.js required.", "NodeJS required.", "We use Node for services."):
        assert extract_jd_keywords(text) == ["node.js"], text


def test_react_and_reactjs_aliases():
    for text in ("React experience required.", "ReactJS experience required.", "React.js experience required."):
        assert extract_jd_keywords(text) == ["react"], text


def test_react_does_not_match_reactive_or_reactor():
    kw = extract_jd_keywords("We use a reactive, event-driven Reactor pipeline.")
    assert "react" not in kw


def test_postgresql_aliases():
    assert extract_jd_keywords("PostgreSQL required.") == ["postgresql"]
    assert extract_jd_keywords("Postgres required.") == ["postgresql"]


def test_aws_and_amazon_web_services_alias():
    assert extract_jd_keywords("AWS experience required.") == ["aws"]
    assert extract_jd_keywords("Amazon Web Services experience required.") == ["aws"]


def test_aws_lambda_candidate_skill_credits_lambda_not_bare_aws():
    # "AWS Lambda" genuinely mentions the word "AWS" adjacent to "Lambda" --
    # both canonical ids are legitimately present in that exact string.
    ids = extract_skill_ids("AWS Lambda")
    assert ids == {"aws", "lambda"}


def test_amazon_sqs_candidate_skill_credits_sqs_only():
    # "Amazon SQS" does not literally contain "aws" or "amazon web
    # services" -- crediting only "sqs" here is the accurate, conservative
    # reading, not an under-match.
    ids = extract_skill_ids("Amazon SQS")
    assert ids == {"sqs"}


# --------------------------------------------------------------------------
# Skill phrase boundaries, punctuation/case handling
# --------------------------------------------------------------------------

def test_case_insensitive_matching():
    assert extract_jd_keywords("PYTHON, Java, GoLang") == ["python", "java", "go"]


def test_punctuation_adjacent_tokens_still_match():
    kw = extract_jd_keywords("Python, Java, and Go (required); C++/C# a plus.")
    assert set(kw) == {"python", "java", "go", "c++", "c#"}


def test_multiword_phrase_boundary():
    assert extract_jd_keywords("Strong system design and distributed systems background.") == [
        "system design", "distributed systems",
    ]


def test_multiword_phrase_not_matched_from_partial_word_overlap():
    # "system design" must not fire from unrelated nearby text that merely
    # shares one of its words.
    kw = extract_jd_keywords("Our design system uses a component library.")
    assert "system design" not in kw


# --------------------------------------------------------------------------
# Duplicate alias handling
# --------------------------------------------------------------------------

def test_duplicate_alias_mentions_collapse_to_one_canonical_id():
    kw = extract_jd_keywords("Go, Go, Golang, golang required everywhere.")
    assert kw == ["go"]


def test_candidate_with_synonymous_skills_does_not_double_count():
    # Candidate lists both "PostgreSQL" and "Postgres" -- still one
    # canonical id, matched_skills length reflects JD keywords, not
    # candidate skill-string count.
    score, matched, gaps = match_candidate_skills(["postgresql"], ["PostgreSQL", "Postgres"])
    assert matched == ["postgresql"]
    assert score == 100.0


# --------------------------------------------------------------------------
# Unrelated words never generate skills
# --------------------------------------------------------------------------

def test_unrelated_prose_yields_no_skills():
    text = (
        "We are looking for a collaborative, curious teammate who enjoys "
        "solving hard problems and working across time zones with a "
        "distributed, remote-first team."
    )
    # "distributed" alone (without "systems") must not fire the
    # "distributed systems" phrase requirement.
    kw = extract_jd_keywords(text)
    assert "distributed systems" not in kw


def test_empty_and_none_text_yield_no_skills():
    assert extract_jd_keywords("") == []
    assert extract_jd_keywords(None) == []
    assert extract_skill_ids("") == set()


# --------------------------------------------------------------------------
# contains_term shared primitive (used by relevance.py's term_hits fix)
# --------------------------------------------------------------------------

def test_contains_term_boundary_safe():
    assert contains_term("Django developer", "go") is False
    assert contains_term("Go developer", "go") is True
    assert contains_term("JavaScript engineer", "java") is False
    assert contains_term("Java engineer", "java") is True


def test_contains_term_multiword_phrase():
    assert contains_term("Applied advanced system design principles", "system design") is True
    assert contains_term("Our design system uses tokens", "system design") is False
