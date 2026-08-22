"""CLAUDE.md Phase 12 sections 37-39: deterministic, browser-free tests for
app.applications.job_identity."""

from app.applications.job_identity import IdentityResult, extract_requisition_token, verify_job_identity


def test_extract_workday_requisition_from_path():
    token = extract_requisition_token(
        "https://acme.wd5.myworkdayjobs.com/External/job/Remote/Software-Engineer_R-1234"
    )
    assert token == "R-1234"


def test_extract_query_param_token():
    token = extract_requisition_token("https://boards.greenhouse.io/acme/jobs?gh_jid=4567890")
    assert token == "4567890"


def test_extract_none_when_no_confident_shape():
    assert extract_requisition_token("https://acme.com/careers") == ""
    assert extract_requisition_token("") == ""


def test_extract_ignores_short_ambiguous_numbers():
    # A bare "2" (e.g. page=2) must never be treated as a requisition id.
    assert extract_requisition_token("https://acme.com/careers?page=2") == ""


def test_verify_match_same_requisition():
    result = verify_job_identity(
        "https://acme.wd5.myworkdayjobs.com/External/job/Remote/Engineer_R-1234",
        "https://acme.wd5.myworkdayjobs.com/External/job/Remote/Engineer_R-1234?step=2",
    )
    assert result.result == IdentityResult.MATCH


def test_verify_mismatch_different_requisition():
    result = verify_job_identity(
        "https://acme.wd5.myworkdayjobs.com/External/job/Remote/Engineer_R-1234",
        "https://acme.wd5.myworkdayjobs.com/External/job/Remote/Recruiter_R-9999",
    )
    assert result.result == IdentityResult.MISMATCH


def test_verify_unverifiable_when_no_token_extractable():
    result = verify_job_identity("https://acme.com/careers/1", "https://acme.com/careers/1/apply")
    assert result.result == IdentityResult.UNVERIFIABLE
