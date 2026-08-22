"""End-to-end acceptance scenarios for Phase 3 (docs/phase3-ats-coverage.md
section 29): each of these drives a real provider connector's fetch_jobs()
output through the full discovery cycle -- dedup, sponsorship/work-arrangement
classification, gates, and (for eligible jobs) resume/package generation --
using deterministic fixtures, no live network access."""

from datetime import datetime, timedelta, timezone

import httpx

from app.agent import cycle as cycle_mod
from app.candidate.profile import save_profile
from app.jobs_repo import list_jobs, list_provenance
from app.models import ApplicationState
from app.providers.ashby import AshbyProvider
from app.providers.smartrecruiters import SmartRecruitersProvider
from app.providers.workable import WorkableProvider
from app.providers.workday import WorkdayProvider


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- Scenario A: Ashby -------------------------------------------------------

def test_scenario_a_ashby_job_flows_through_full_pipeline(tmp_env, sample_profile, monkeypatch):
    save_profile(sample_profile)

    def handler(request):
        return httpx.Response(200, json={"organizationName": "Ashby Acme", "jobs": [{
            "id": "job-1", "title": "Backend Software Engineer", "location": "Remote (US)",
            "isRemote": True,
            "descriptionPlain": (
                "Build REST APIs in Python with FastAPI and PostgreSQL, deployed via "
                "Docker with CI/CD. Fully remote. Visa sponsorship available. 3+ years experience."
            ),
            "employmentType": "FullTime", "publishedAt": "2026-08-21T00:00:00Z",
            "applyUrl": "https://jobs.ashbyhq.com/ashby-acme/job-1/apply",
            "jobUrl": "https://jobs.ashbyhq.com/ashby-acme/job-1",
        }]})

    provider = AshbyProvider(["ashby-acme"], client=_client(handler))
    monkeypatch.setattr(cycle_mod, "get_enabled_providers", lambda: [provider])

    summary = cycle_mod.run_discovery_cycle()
    assert summary["jobs_new"] == 1

    jobs = list_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.provider == "ashby"
    assert job.application_state == ApplicationState.READY_TO_APPLY
    assert job.resume_docx_path and job.resume_pdf_path and job.resume_txt_path

    provenance = list_provenance(job.id)
    assert len(provenance) == 1
    assert provenance[0]["provider"] == "ashby"

    # Re-running the cycle with the same fixture must dedupe, not duplicate.
    summary2 = cycle_mod.run_discovery_cycle()
    assert summary2["jobs_new"] == 0
    assert summary2["jobs_deduplicated"] == 1
    assert len(list_jobs()) == 1


# --- Scenario B: Workable ----------------------------------------------------

def test_scenario_b_workable_job_flows_through_full_pipeline(tmp_env, sample_profile, monkeypatch):
    save_profile(sample_profile)

    def handler(request):
        if "/jobs/" in str(request.url):
            return httpx.Response(200, json={
                "description": (
                    "Build REST APIs in Python with FastAPI and PostgreSQL, using Docker "
                    "and CI/CD pipelines. We sponsor work visas. 3+ years experience."
                ),
                "requirements": "", "benefits": "",
            })
        if request.url.params.get("page", "1") == "1":
            return httpx.Response(200, json={"jobs": [{
                "title": "Python Developer", "shortcode": "PY1", "employment_type": "full",
                "telecommute": True, "city": "Remote", "country": "United States",
                "published_on": "2026-08-20",
                "url": "https://apply.workable.com/workable-acme/j/PY1/",
            }]})
        return httpx.Response(200, json={"jobs": []})

    provider = WorkableProvider(["workable-acme"], client=_client(handler))
    monkeypatch.setattr(cycle_mod, "get_enabled_providers", lambda: [provider])

    summary = cycle_mod.run_discovery_cycle()
    assert summary["jobs_new"] == 1
    jobs = list_jobs()
    assert jobs[0].provider == "workable"
    assert jobs[0].application_state in (ApplicationState.READY_TO_APPLY, ApplicationState.REVIEW_REQUIRED)


# --- Scenario C: SmartRecruiters ---------------------------------------------

def test_scenario_c_smartrecruiters_job_flows_through_full_pipeline(tmp_env, sample_profile, monkeypatch):
    save_profile(sample_profile)

    def handler(request):
        url = str(request.url)
        if url.endswith("/postings/sr1"):
            return httpx.Response(200, json={"jobAd": {"sections": {
                "jobDescription": {"text": (
                    "Build REST APIs in Python with FastAPI, PostgreSQL, Docker, CI/CD. "
                    "Visa sponsorship available. 3+ years experience."
                )},
            }}})
        offset = int(request.url.params.get("offset", "0"))
        if offset == 0:
            return httpx.Response(200, json={"totalFound": 1, "content": [{
                "id": "sr1", "name": "Backend Engineer", "company": {"name": "SR Acme"},
                "location": {"city": "Remote", "country": "us", "remote": True},
                "typeOfEmployment": {"label": "Full-time"},
                "postingUrl": "https://jobs.smartrecruiters.com/SRAcme/sr1",
                # Relative to "now" (not a hardcoded date) so this test never
                # flakes as real wall-clock time passes and a fixed past date
                # ages past FRESHNESS_MAX_DAYS -- pre-existing flakiness
                # unrelated to Phase 6, fixed here since it's a one-line,
                # backward-compatible test-data change (CLAUDE.md section 51).
                "releasedDate": (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            }]})
        return httpx.Response(200, json={"totalFound": 1, "content": []})

    provider = SmartRecruitersProvider(["sr-acme"], client=_client(handler))
    monkeypatch.setattr(cycle_mod, "get_enabled_providers", lambda: [provider])

    summary = cycle_mod.run_discovery_cycle()
    assert summary["jobs_new"] == 1
    jobs = list_jobs()
    assert jobs[0].provider == "smartrecruiters"
    assert jobs[0].application_state in (ApplicationState.READY_TO_APPLY, ApplicationState.REVIEW_REQUIRED)


# --- Scenario G: unsupported/limited Workday tenant --------------------------

def test_scenario_g_workday_limited_tenant_fails_clean_no_fabrication(tmp_env, sample_profile, monkeypatch):
    save_profile(sample_profile)

    def handler(request):
        return httpx.Response(403, text="blocked by bot protection")

    provider = WorkdayProvider(
        ["https://blocked.wd5.myworkdayjobs.com/wday/cxs/blocked/External"], client=_client(handler),
    )
    monkeypatch.setattr(cycle_mod, "get_enabled_providers", lambda: [provider])

    summary = cycle_mod.run_discovery_cycle()
    assert summary["jobs_fetched"] == 0
    assert summary["jobs_new"] == 0
    assert list_jobs() == []
    # A clean empty result is not reported as an "error" -- it's the
    # documented PARTIAL-support behavior, not a crash.
    assert summary["errors"] == []
