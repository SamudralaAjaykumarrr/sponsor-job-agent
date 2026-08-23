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


# --- Lever/Ashby real id shape: a UUID path segment, never numeric/"R-"
# prefixed -- _PATH_REQ_RE alone never matches either provider's real posting
# ids (verified live against both APIs).

def test_extract_uuid_token_lever_shaped_url():
    token = extract_requisition_token("https://jobs.lever.co/leverdemo/33538a2f-d27d-4a96-8f05-fa4b0e4d940e")
    assert token == "33538A2F-D27D-4A96-8F05-FA4B0E4D940E"


def test_extract_uuid_token_lever_apply_suffix_still_matches():
    token = extract_requisition_token(
        "https://jobs.lever.co/leverdemo/33538a2f-d27d-4a96-8f05-fa4b0e4d940e/apply"
    )
    assert token == "33538A2F-D27D-4A96-8F05-FA4B0E4D940E"


def test_extract_uuid_token_ashby_shaped_url():
    token = extract_requisition_token(
        "https://jobs.ashbyhq.com/ashby/7458d4e9-da2e-47bd-98cb-adfda43d42b2/application"
    )
    assert token == "7458D4E9-DA2E-47BD-98CB-ADFDA43D42B2"


def test_verify_match_same_lever_uuid_requisition():
    result = verify_job_identity(
        "https://jobs.lever.co/leverdemo/33538a2f-d27d-4a96-8f05-fa4b0e4d940e",
        "https://jobs.lever.co/leverdemo/33538a2f-d27d-4a96-8f05-fa4b0e4d940e/apply",
    )
    assert result.result == IdentityResult.MATCH


def test_verify_mismatch_different_ashby_uuid_requisition():
    result = verify_job_identity(
        "https://jobs.ashbyhq.com/ashby/7458d4e9-da2e-47bd-98cb-adfda43d42b2/application",
        "https://jobs.ashbyhq.com/ashby/00000000-0000-0000-0000-000000000000/application",
    )
    assert result.result == IdentityResult.MISMATCH


def test_extract_uuid_does_not_match_short_hex_fragment():
    # Must never partially match a substring of a longer/unrelated token.
    assert extract_requisition_token("https://acme.com/careers/da2e-47bd") == ""
