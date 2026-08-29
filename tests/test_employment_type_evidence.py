"""Employment Type Evidence Hardening V1 deterministic test matrix.

No real network anywhere in this file: `fetch_structured_page_employment_type`
is always called either with a `client=` built on `httpx.MockTransport`
(matching tests/test_providers_workable.py's existing pattern) or is never
reached at all (mock_ats jobs skip the live fetch entirely, matching every
other module's mock_ats exclusion). No real submission."""

import httpx
import pytest

from app.applications.canary_feasibility import FeasibilityVerdict, evaluate_canary_feasibility
from app.applications.employment_type_evidence import (
    extract_jobposting_employment_type,
    fetch_structured_page_employment_type,
    refresh_page_evidence,
)
from app.jobs_repo import get_job, insert_job, update_job
from app.matching.employment_type import (
    EmploymentTypeEvidenceSource,
    classify_employment_type,
    normalize_structured_page_employment_type,
    resolve_employment_type_evidence,
)
from app.models import ApplicationState, EmploymentType, Job, SponsorshipStatus


def _client_returning(body: str, status: int = 200) -> httpx.Client:
    def handler(request):
        return httpx.Response(status, text=body)
    return httpx.Client(transport=httpx.MockTransport(handler))


def _client_raising() -> httpx.Client:
    def handler(request):
        raise httpx.ConnectTimeout("no route to host", request=request)
    return httpx.Client(transport=httpx.MockTransport(handler))


def _jobposting_html(employment_type: str = "FULL_TIME", extra: str = "") -> str:
    return f"""<html><head>
    <script type="application/ld+json">
    {{"@context":"https://schema.org/","@type":"JobPosting","title":"Backend Engineer",
      "employmentType":"{employment_type}"{extra}}}
    </script>
    </head><body>real posting content</body></html>"""


# --- 1: explicit textual FULL_TIME ------------------------------------------

def test_explicit_jd_text_full_time():
    d = resolve_employment_type_evidence("", "Backend Engineer", "This is a full-time role.")
    assert d.value == EmploymentType.FULL_TIME
    assert d.source == EmploymentTypeEvidenceSource.JD_TEXT


# --- 2: provider structured FULL_TIME + silent JD ---------------------------

def test_provider_structured_full_time_silent_jd():
    d = resolve_employment_type_evidence("Full-time", "Backend Engineer", "Build things at scale.")
    assert d.value == EmploymentType.FULL_TIME
    assert d.source == EmploymentTypeEvidenceSource.PROVIDER_STRUCTURED
    assert d.raw_value == "Full-time"


# --- 3: JSON-LD FULL_TIME + silent JD + silent provider ---------------------

def test_jsonld_full_time_silent_jd_and_provider():
    d = resolve_employment_type_evidence("", "Backend Engineer", "Build things at scale.",
                                          structured_page_value="FULL_TIME")
    assert d.value == EmploymentType.FULL_TIME
    assert d.source == EmploymentTypeEvidenceSource.STRUCTURED_PAGE_JSONLD
    assert d.raw_value == "FULL_TIME"


# --- 4-7: structured CONTRACT / PART_TIME / TEMPORARY / INTERN (JSON-LD) ---

@pytest.mark.parametrize("jsonld_value,expected", [
    ("CONTRACTOR", EmploymentType.CONTRACT),
    ("PART_TIME", EmploymentType.PART_TIME),
    ("TEMPORARY", EmploymentType.TEMPORARY),
    ("INTERN", EmploymentType.INTERNSHIP),
])
def test_structured_negative_jsonld_types(jsonld_value, expected):
    d = resolve_employment_type_evidence("", "Engineer", "Build things.", structured_page_value=jsonld_value)
    assert d.value == expected
    assert d.source == EmploymentTypeEvidenceSource.STRUCTURED_PAGE_JSONLD


# --- 8: FULL_TIME metadata conflicting with explicit contract text ---------

def test_explicit_contract_text_overrides_structured_full_time():
    d = resolve_employment_type_evidence("Full-time", "Engineer", "This is a contract position, 6 months.")
    assert d.value == EmploymentType.CONTRACT
    assert d.source == EmploymentTypeEvidenceSource.JD_TEXT
    assert "overrides" in d.reason


def test_jsonld_full_time_conflicting_with_explicit_contract_text_also_negative_wins():
    d = resolve_employment_type_evidence("", "Engineer", "This is a contract position.",
                                          structured_page_value="FULL_TIME")
    assert d.value == EmploymentType.CONTRACT
    assert d.source == EmploymentTypeEvidenceSource.JD_TEXT


def test_provider_positive_page_negative_no_jd_tiebreaker_negative_still_wins():
    # provider says FULL_TIME, JSON-LD page says CONTRACTOR, JD silent --
    # explicit contradictory evidence must not be IGNORED (it's named in
    # the reason), but under this project's own safety-first "any negative
    # wins" policy it is not averaged away or silently guessed toward
    # FULL_TIME either -- the negative decides, full stop.
    d = resolve_employment_type_evidence("Full-time", "Engineer", "Build things.",
                                          structured_page_value="CONTRACTOR")
    assert d.value == EmploymentType.CONTRACT
    assert d.source == EmploymentTypeEvidenceSource.STRUCTURED_PAGE_JSONLD
    assert "overrides" in d.reason


def test_two_disagreeing_negative_subtypes_never_weaken_to_unknown():
    # provider says CONTRACT, JSON-LD page says PART_TIME, JD silent -- both
    # sources agree it's NOT full-time, they just disagree on which
    # subtype. This must NEVER resolve to UNKNOWN: app.applications.
    # eligibility hard_skips a specific negative type but treats bare
    # UNKNOWN as ASSIST-only/enters_queue=True, so downgrading here would
    # perversely weaken the hard-skip gate for a job that is unambiguously
    # not full-time (a real regression this exact case caught live).
    d = resolve_employment_type_evidence("Contractor", "Engineer", "Build things.",
                                          structured_page_value="PART_TIME")
    assert d.value != EmploymentType.UNKNOWN
    assert d.value == EmploymentType.CONTRACT
    assert d.source == EmploymentTypeEvidenceSource.PROVIDER_STRUCTURED
    assert "different non-full-time subtype" in d.reason


def test_jd_c2c_text_and_provider_contract_field_both_negative_never_weakens_to_unknown():
    # Real regression caught live by this feature's own full-suite run
    # (tests/test_apply_automation_settings.py::
    # test_non_full_time_cannot_be_enabled_for_unattended_via_preferences):
    # JD text says "C2C contract" (matches the C2C token before CONTRACT in
    # _NEGATIVE_TYPE_SIGNALS' own priority order) while the provider raw
    # field says "Contract" -- two different negative TYPES from two
    # sources, but JD text is decisive on its own and must never fall
    # through to a provider-vs-page conflict/UNKNOWN path.
    d = resolve_employment_type_evidence("Contract", "Backend Software Engineer",
                                          "This is a 6-month C2C contract position.")
    assert d.value != EmploymentType.UNKNOWN
    assert d.value == EmploymentType.C2C
    assert d.source == EmploymentTypeEvidenceSource.JD_TEXT


# --- 9: no evidence anywhere -> UNKNOWN -------------------------------------

def test_no_evidence_anywhere_is_unknown():
    d = resolve_employment_type_evidence("", "Engineer", "Build things at scale.")
    assert d.value == EmploymentType.UNKNOWN
    assert d.source == EmploymentTypeEvidenceSource.NONE


# --- 10: salary/benefits alone -> UNKNOWN -----------------------------------

def test_salary_and_benefits_alone_never_infer_full_time():
    description = (
        "Compensation: $180,000 - $210,000 USD. We offer excellent benefits including "
        "health insurance, 401k matching, and unlimited PTO. Based in our downtown office."
    )
    d = resolve_employment_type_evidence("", "Software Engineer", description)
    assert d.value == EmploymentType.UNKNOWN
    assert d.source == EmploymentTypeEvidenceSource.NONE


def test_title_alone_never_infers_full_time():
    # Title "lacks the word contract" -- must still never be treated as a
    # positive signal by itself.
    d = resolve_employment_type_evidence("", "Senior Backend Software Engineer", "")
    assert d.value == EmploymentType.UNKNOWN


# --- 11: provider value normalization ---------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Full-time", EmploymentType.FULL_TIME),
    ("FULL_TIME", EmploymentType.FULL_TIME),
    ("Part time", EmploymentType.PART_TIME),
    ("Contractor", EmploymentType.CONTRACT),
    ("Temporary position", EmploymentType.TEMPORARY),
    ("Internship", EmploymentType.INTERNSHIP),
])
def test_provider_raw_field_normalization_matches_old_classifier(raw, expected):
    # classify_employment_type (unchanged, Phase 8 contract) and
    # resolve_employment_type_evidence must never diverge on a single-source
    # provider-raw-field case with no JD/page evidence.
    assert classify_employment_type(raw, "Engineer", "") == expected
    d = resolve_employment_type_evidence(raw, "Engineer", "")
    assert d.value == expected
    assert d.source == EmploymentTypeEvidenceSource.PROVIDER_STRUCTURED


@pytest.mark.parametrize("jsonld_raw,expected", [
    ("FULL_TIME", EmploymentType.FULL_TIME),
    ("full_time", EmploymentType.FULL_TIME),
    ("Full Time", EmploymentType.FULL_TIME),
    ("PART_TIME", EmploymentType.PART_TIME),
    ("VOLUNTEER", None),
    ("", None),
    ("SOMETHING_UNRECOGNIZED", None),
])
def test_normalize_structured_page_employment_type(jsonld_raw, expected):
    assert normalize_structured_page_employment_type(jsonld_raw) == expected


# --- JSON-LD page HTML extraction (pure parsing) ----------------------------

def test_extract_jsonld_direct_jobposting_object():
    html = _jobposting_html("FULL_TIME")
    assert extract_jobposting_employment_type(html) == "FULL_TIME"


def test_extract_jsonld_returns_empty_when_no_ld_json_block():
    html = "<html><body>No structured data here at all.</body></html>"
    assert extract_jobposting_employment_type(html) == ""


def test_extract_jsonld_ignores_malformed_json():
    html = '<script type="application/ld+json">{not valid json,,,}</script>'
    assert extract_jobposting_employment_type(html) == ""


def test_extract_jsonld_ignores_non_jobposting_type():
    html = ('<script type="application/ld+json">'
            '{"@type":"Organization","employmentType":"FULL_TIME"}</script>')
    assert extract_jobposting_employment_type(html) == ""


def test_extract_jsonld_handles_array_of_ld_objects():
    html = ('<script type="application/ld+json">'
            '[{"@type":"BreadcrumbList"},{"@type":"JobPosting","employmentType":"PART_TIME"}]'
            '</script>')
    assert extract_jobposting_employment_type(html) == "PART_TIME"


def test_extract_jsonld_handles_graph_wrapper():
    html = ('<script type="application/ld+json">'
            '{"@context":"https://schema.org","@graph":['
            '{"@type":"WebPage"},{"@type":"JobPosting","employmentType":"TEMPORARY"}]}'
            '</script>')
    assert extract_jobposting_employment_type(html) == "TEMPORARY"


def test_extract_jsonld_handles_employment_type_as_list():
    html = ('<script type="application/ld+json">'
            '{"@type":"JobPosting","employmentType":["FULL_TIME"]}</script>')
    assert extract_jobposting_employment_type(html) == "FULL_TIME"


def test_extract_jsonld_never_scans_arbitrary_page_prose():
    # The word "full-time" appears in ordinary page text OUTSIDE any
    # ld+json block -- must never be picked up as structured evidence.
    html = "<html><body><p>This is a full-time position!</p></body></html>"
    assert extract_jobposting_employment_type(html) == ""


# --- fetch_structured_page_employment_type (network-shaped, mocked) --------

def test_fetch_structured_page_employment_type_success():
    client = _client_returning(_jobposting_html("FULL_TIME"))
    value = fetch_structured_page_employment_type("https://example.com/careers/1", client=client)
    assert value == "FULL_TIME"


def test_fetch_structured_page_employment_type_network_failure_returns_empty_never_raises():
    client = _client_raising()
    value = fetch_structured_page_employment_type("https://example.com/careers/1", client=client)
    assert value == ""


def test_fetch_structured_page_employment_type_empty_url_returns_empty():
    assert fetch_structured_page_employment_type("") == ""


def test_fetch_structured_page_employment_type_http_error_returns_empty():
    client = _client_returning("not found", status=404)
    value = fetch_structured_page_employment_type("https://example.com/careers/gone", client=client)
    assert value == ""


# --- persistence / refresh --------------------------------------------------

def _make_job(tmp_env, **overrides) -> Job:
    defaults = dict(
        title="Backend Engineer", company="Acme Corp", location="Remote - US",
        description="Build things.", provider="greenhouse",
        canonical_url="https://boards.greenhouse.io/acme/jobs/1",
        url="https://boards.greenhouse.io/acme/jobs/1",
        sponsorship_status=SponsorshipStatus.CONFIRMED_SPONSOR,
        application_state=ApplicationState.ANALYZED,
    )
    defaults.update(overrides)
    job_id = insert_job(Job(**defaults))
    return get_job(job_id)


def test_refresh_page_evidence_persists_raw_value(tmp_env):
    job = _make_job(tmp_env)
    client = _client_returning(_jobposting_html("FULL_TIME"))
    raw = refresh_page_evidence(job, client=client)
    assert raw == "FULL_TIME"
    reloaded = get_job(job.id)
    assert reloaded.employment_type_page_evidence_raw == "FULL_TIME"
    assert reloaded.employment_type_page_evidence_checked_at != ""


def test_refresh_page_evidence_persists_empty_string_on_no_signal(tmp_env):
    job = _make_job(tmp_env)
    client = _client_returning("<html><body>nothing here</body></html>")
    raw = refresh_page_evidence(job, client=client)
    assert raw == ""
    reloaded = get_job(job.id)
    assert reloaded.employment_type_page_evidence_raw == ""
    # "checked, found nothing" is still distinguishable from "never checked"
    assert reloaded.employment_type_page_evidence_checked_at != ""


# --- 16: serialization/restart preserves classification evidence -----------

def test_persisted_evidence_survives_reload_and_still_resolves_correctly(tmp_env):
    job = _make_job(tmp_env, employment_type="", description="Build things.")
    update_job(job.id, employment_type_page_evidence_raw="FULL_TIME",
               employment_type_page_evidence_checked_at="2026-08-29T00:00:00+00:00")
    # Simulate a fresh process / restart: reload strictly from the DB row.
    reloaded = get_job(job.id)
    assert reloaded.employment_type_page_evidence_raw == "FULL_TIME"
    d = resolve_employment_type_evidence(
        reloaded.employment_type, reloaded.title, reloaded.description,
        reloaded.employment_type_page_evidence_raw,
    )
    assert d.value == EmploymentType.FULL_TIME
    assert d.source == EmploymentTypeEvidenceSource.STRUCTURED_PAGE_JSONLD


# --- 14: unrelated provider metadata cannot contaminate another job --------

def test_page_evidence_is_per_job_never_shared(tmp_env):
    job_a = _make_job(tmp_env, external_job_id="a1")
    job_b = _make_job(tmp_env, external_job_id="b1")
    update_job(job_a.id, employment_type_page_evidence_raw="FULL_TIME")
    update_job(job_b.id, employment_type_page_evidence_raw="CONTRACTOR")

    reloaded_a = get_job(job_a.id)
    reloaded_b = get_job(job_b.id)
    assert reloaded_a.employment_type_page_evidence_raw == "FULL_TIME"
    assert reloaded_b.employment_type_page_evidence_raw == "CONTRACTOR"

    decision_a = resolve_employment_type_evidence(reloaded_a.employment_type, reloaded_a.title,
                                                    reloaded_a.description,
                                                    reloaded_a.employment_type_page_evidence_raw)
    decision_b = resolve_employment_type_evidence(reloaded_b.employment_type, reloaded_b.title,
                                                    reloaded_b.description,
                                                    reloaded_b.employment_type_page_evidence_raw)
    assert decision_a.value == EmploymentType.FULL_TIME
    assert decision_b.value == EmploymentType.CONTRACT


# --- 12/13: feasibility gate integration ------------------------------------

def test_feasibility_gate_accepts_evidence_backed_full_time_from_page(tmp_env, monkeypatch):
    job = _make_job(tmp_env, employment_type="", title="Backend Software Engineer",
                     description="Build and operate our core services with Python and AWS.",
                     technical_match_score=80.0, matched_skills="python,aws", gap_skills="")
    monkeypatch.setattr(
        "app.applications.canary_feasibility.refresh_page_evidence",
        lambda j: "FULL_TIME",
    )
    result = evaluate_canary_feasibility(job)
    assert result.employment_type.verdict == FeasibilityVerdict.PASS
    assert "STRUCTURED_PAGE_JSONLD" in result.employment_type.reason


def test_feasibility_gate_reviews_unknown_when_no_evidence_anywhere(tmp_env, monkeypatch):
    job = _make_job(tmp_env, employment_type="", title="Backend Software Engineer",
                     description="Build and operate our core services.",
                     technical_match_score=80.0, matched_skills="python", gap_skills="")
    monkeypatch.setattr(
        "app.applications.canary_feasibility.refresh_page_evidence",
        lambda j: "",
    )
    result = evaluate_canary_feasibility(job)
    assert result.employment_type.verdict == FeasibilityVerdict.REVIEW
    assert "not positively confirmed" in result.employment_type.reason


def test_feasibility_gate_rejects_structured_contract_from_page(tmp_env, monkeypatch):
    job = _make_job(tmp_env, employment_type="", title="Backend Software Engineer",
                     description="Build and operate our core services.",
                     technical_match_score=80.0, matched_skills="python", gap_skills="")
    monkeypatch.setattr(
        "app.applications.canary_feasibility.refresh_page_evidence",
        lambda j: "CONTRACTOR",
    )
    result = evaluate_canary_feasibility(job)
    assert result.employment_type.verdict == FeasibilityVerdict.REJECT


def test_feasibility_gate_never_fetches_network_for_mock_ats_jobs(tmp_env, monkeypatch):
    job = _make_job(tmp_env, provider="mock_ats", employment_type="Full-time",
                     canonical_url="https://example.com/jobs/1", url="https://example.com/jobs/1",
                     technical_match_score=80.0, matched_skills="python", gap_skills="")

    def _boom(_job):
        raise AssertionError("refresh_page_evidence must never be called for mock_ats jobs")

    monkeypatch.setattr("app.applications.canary_feasibility.refresh_page_evidence", _boom)
    result = evaluate_canary_feasibility(job)
    assert result.employment_type.verdict == FeasibilityVerdict.PASS


# --- eligibility.py / approval.py: persisted evidence, never a live fetch --

def test_eligibility_gate_uses_persisted_page_evidence_without_network(tmp_env):
    from app.applications.eligibility import evaluate_executor_eligibility

    job = _make_job(tmp_env, employment_type="", title="Backend Software Engineer",
                     description="Build things.", location="Remote - US",
                     application_state=ApplicationState.READY_TO_APPLY)
    update_job(job.id, employment_type_page_evidence_raw="FULL_TIME")
    reloaded = get_job(job.id)
    # No network client is ever constructed by evaluate_executor_eligibility
    # -- if it tried, this fake, unroutable URL below would hang/raise.
    reloaded.canonical_url = "https://this-host-does-not-resolve.invalid/job/1"
    result = evaluate_executor_eligibility(reloaded)
    assert result.employment_type == EmploymentType.FULL_TIME


def test_eligibility_gate_explicit_contract_text_overrides_stale_full_time_page_evidence(tmp_env):
    from app.applications.eligibility import evaluate_executor_eligibility

    # Regression guard for the real gap Employment Type Evidence Hardening V1
    # fixes: a positive structured signal (here, page evidence) must never
    # silently win over an explicit negative JD text signal.
    job = _make_job(tmp_env, employment_type="", title="Backend Engineer",
                     description="This is a 6-month contract position.")
    update_job(job.id, employment_type_page_evidence_raw="FULL_TIME")
    reloaded = get_job(job.id)
    result = evaluate_executor_eligibility(reloaded)
    assert result.employment_type == EmploymentType.CONTRACT
    assert result.hard_skip is True
