from app.providers.detector import detect_provider


def test_greenhouse_url_detected_with_tenant():
    r = detect_provider("https://job-boards.greenhouse.io/acme/jobs/12345")
    assert r.provider == "greenhouse"
    assert r.tenant_identifier == "acme"
    assert r.confidence >= 0.9


def test_greenhouse_api_shaped_url_extracts_tenant_not_api_version():
    """Real bug caught during Phase 6 live acquisition validation: a real
    company's (Duolingo's) careers page linked directly to
    boards-api.greenhouse.io/v1/boards/{tenant}/departments -- the same
    boards-api.greenhouse.io host app.providers.greenhouse.GREENHOUSE_JOBS_URL
    itself uses, but with 2 extra path segments ("v1", "boards") before the
    tenant. Before the fix, "v1" was extracted as the tenant instead of the
    real company slug."""
    r = detect_provider("https://boards-api.greenhouse.io/v1/boards/duolingo/departments")
    assert r.provider == "greenhouse"
    assert r.tenant_identifier == "duolingo"
    assert r.confidence >= 0.9


def test_greenhouse_api_jobs_url_also_extracts_correct_tenant():
    r = detect_provider("https://boards-api.greenhouse.io/v1/boards/acme/jobs")
    assert r.provider == "greenhouse"
    assert r.tenant_identifier == "acme"


def test_lever_url_detected_with_tenant():
    r = detect_provider("https://jobs.lever.co/acme/abc-123")
    assert r.provider == "lever"
    assert r.tenant_identifier == "acme"
    assert r.confidence >= 0.9


def test_ashby_url_detected_with_tenant():
    r = detect_provider("https://jobs.ashbyhq.com/acme/job-id")
    assert r.provider == "ashby"
    assert r.tenant_identifier == "acme"


def test_workable_apply_domain_detected():
    r = detect_provider("https://apply.workable.com/acme-inc/j/ABCDEF/")
    assert r.provider == "workable"
    assert r.tenant_identifier == "acme-inc"


def test_workable_subdomain_detected():
    r = detect_provider("https://acme.workable.com/j/ABCDEF/")
    assert r.provider == "workable"
    assert r.tenant_identifier == "acme"


def test_smartrecruiters_detected():
    r = detect_provider("https://careers.smartrecruiters.com/AcmeCorp/software-engineer")
    assert r.provider == "smartrecruiters"
    assert r.tenant_identifier == "AcmeCorp"


def test_bamboohr_detected():
    r = detect_provider("https://acme.bamboohr.com/careers/42")
    assert r.provider == "bamboohr"
    assert r.tenant_identifier == "acme"


def test_bamboohr_non_careers_path_not_detected():
    r = detect_provider("https://acme.bamboohr.com/login")
    assert r.provider is None


def test_recruitee_detected():
    r = detect_provider("https://acme.recruitee.com/o/software-engineer")
    assert r.provider == "recruitee"
    assert r.tenant_identifier == "acme"


def test_teamtailor_detected_low_confidence_unsupported_downstream():
    r = detect_provider("https://acme.teamtailor.com/jobs/123-software-engineer")
    assert r.provider == "teamtailor"
    assert r.tenant_identifier == "acme"


def test_pinpoint_detected():
    r = detect_provider("https://acme.pinpointhq.com/postings/xyz")
    assert r.provider == "pinpoint"


def test_breezy_detected():
    r = detect_provider("https://acme.breezy.hr/p/abcdef-software-engineer")
    assert r.provider == "breezy"
    assert r.tenant_identifier == "acme"


def test_jazzhr_detected():
    r = detect_provider("https://acme.applytojob.com/apply/abcdef/Software-Engineer")
    assert r.provider == "jazzhr"
    assert r.tenant_identifier == "acme"


def test_jobvite_detected():
    r = detect_provider("https://jobs.jobvite.com/acme/job/abc123")
    assert r.provider == "jobvite"
    assert r.tenant_identifier == "acme"


def test_workday_detected_with_tenant_and_site():
    r = detect_provider("https://acme.wd5.myworkdayjobs.com/External/job/Remote/Software-Engineer_R-1234")
    assert r.provider == "workday"
    assert "acme" in r.tenant_identifier
    assert "wd5" in r.tenant_identifier
    assert "External" in r.tenant_identifier


def test_icims_detected_low_confidence():
    r = detect_provider("https://acme.icims.com/jobs/1234/software-engineer/job")
    assert r.provider == "icims"
    assert r.confidence < 0.7  # LIMITED -- never reported as certain


def test_oracle_detected_no_tenant_extractable():
    r = detect_provider("https://acme.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/12345")
    assert r.provider == "oracle"
    assert r.tenant_identifier is None
    assert r.confidence < 0.7


def test_unknown_url_returns_no_match():
    r = detect_provider("https://example.com/careers/software-engineer")
    assert r.provider is None
    assert r.confidence == 0.0
    assert r.tenant_identifier is None


def test_malformed_url_does_not_raise():
    r = detect_provider("not a url at all")
    assert r.provider is None


def test_empty_url_returns_no_match():
    r = detect_provider("")
    assert r.provider is None


def test_no_low_confidence_result_reported_as_certain():
    """Every match must self-report a confidence < 1.0 unless a tenant is
    deterministically extracted -- never claim certainty on a bare host match."""
    r = detect_provider("https://acme.icims.com/")
    assert r.confidence < 1.0
