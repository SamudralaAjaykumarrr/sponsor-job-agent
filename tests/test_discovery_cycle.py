from datetime import datetime, timezone

from app.agent import cycle as cycle_mod
from app.candidate.profile import save_profile
from app.jobs_repo import list_jobs
from app.models import ApplicationState
from app.providers.base import JobProvider, RawJobPosting


class FakeProvider(JobProvider):
    name = "fake"

    def __init__(self, jobs: list[RawJobPosting]):
        self._jobs = jobs

    def fetch_jobs(self, max_jobs: int) -> list[RawJobPosting]:
        return self._jobs[:max_jobs]


class FailingProvider(JobProvider):
    name = "failing"

    def fetch_jobs(self, max_jobs: int) -> list[RawJobPosting]:
        raise RuntimeError("simulated provider outage")


NOW = datetime.now(timezone.utc).isoformat()

CONFIRMED_REMOTE = RawJobPosting(
    provider="fake", external_job_id="gh-1", title="Backend Software Engineer",
    company="Acme Corp", location="Remote (US)",
    description=(
        "We are hiring a Backend Software Engineer to build REST APIs in Python "
        "using FastAPI and PostgreSQL, deployed via Docker with CI/CD pipelines. "
        "This is a fully remote position. Visa sponsorship available for "
        "qualified candidates. 3+ years of experience required."
    ),
    url="https://example.com/jobs/gh-1", published_at=NOW,
)

LIKELY_SPONSOR = RawJobPosting(
    provider="fake", external_job_id="gh-2", title="Python Developer",
    company="Acme Corp", location="Remote, US",
    description=(
        "Join our backend team building APIs with Python, Django, PostgreSQL, "
        "and Docker. 2-4 years of experience."
    ),
    url="https://example.com/jobs/gh-2", published_at=NOW,
)

NO_SPONSORSHIP = RawJobPosting(
    provider="fake", external_job_id="gh-3", title="Backend Engineer",
    company="NoSponsorCo", location="Remote (US)",
    description=(
        "We build REST APIs in Python and PostgreSQL. We are unable to sponsor "
        "visas for this position, now or in the future."
    ),
    url="https://example.com/jobs/gh-3", published_at=NOW,
)

OVERLY_SENIOR = RawJobPosting(
    provider="fake", external_job_id="gh-4", title="Principal Software Engineer",
    company="Acme Corp", location="Remote (US)",
    description=(
        "We need a Principal engineer with 10+ years of experience in Python, "
        "PostgreSQL, and Docker. Visa sponsorship available."
    ),
    url="https://example.com/jobs/gh-4", published_at=NOW,
)


def _run(monkeypatch, jobs):
    monkeypatch.setattr(cycle_mod, "get_enabled_providers", lambda: [FakeProvider(jobs)])
    return cycle_mod.run_discovery_cycle()


def test_confirmed_sponsor_remote_reaches_ready_to_apply(tmp_env, sample_profile, monkeypatch):
    save_profile(sample_profile)
    summary = _run(monkeypatch, [CONFIRMED_REMOTE])
    jobs = list_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.application_state == ApplicationState.READY_TO_APPLY
    assert job.resume_docx_path and job.resume_pdf_path and job.resume_txt_path
    assert summary["confirmed_sponsors"] == 1
    assert summary["packages_generated"] == 1


def test_likely_sponsor_reaches_review_required(tmp_env, sample_profile, monkeypatch):
    save_profile(sample_profile)
    summary = _run(monkeypatch, [LIKELY_SPONSOR])
    jobs = list_jobs()
    assert len(jobs) == 1
    assert jobs[0].application_state == ApplicationState.REVIEW_REQUIRED
    assert jobs[0].resume_docx_path
    assert summary["likely_sponsors"] == 1


def test_no_sponsorship_is_hard_skipped(tmp_env, sample_profile, monkeypatch):
    save_profile(sample_profile)
    _run(monkeypatch, [NO_SPONSORSHIP])
    jobs = list_jobs()
    assert len(jobs) == 1
    assert jobs[0].application_state == ApplicationState.SKIPPED_NO_SPONSORSHIP
    assert jobs[0].resume_docx_path is None


def test_overly_senior_role_is_skipped(tmp_env, sample_profile, monkeypatch):
    save_profile(sample_profile)
    _run(monkeypatch, [OVERLY_SENIOR])
    jobs = list_jobs()
    assert len(jobs) == 1
    assert jobs[0].application_state == ApplicationState.SKIPPED_SENIORITY
    assert jobs[0].resume_docx_path is None


def test_duplicate_posting_is_stored_once(tmp_env, sample_profile, monkeypatch):
    save_profile(sample_profile)
    _run(monkeypatch, [CONFIRMED_REMOTE])
    assert len(list_jobs()) == 1

    summary2 = _run(monkeypatch, [CONFIRMED_REMOTE])
    jobs = list_jobs()
    assert len(jobs) == 1  # still just one row -- no duplicate application package
    assert summary2["jobs_new"] == 0
    assert summary2["jobs_deduplicated"] == 1


def test_full_cycle_all_five_scenarios_together(tmp_env, sample_profile, monkeypatch):
    save_profile(sample_profile)
    summary = _run(monkeypatch, [CONFIRMED_REMOTE, LIKELY_SPONSOR, NO_SPONSORSHIP, OVERLY_SENIOR])
    jobs = {j.external_job_id: j for j in list_jobs()}
    assert len(jobs) == 4
    assert jobs["gh-1"].application_state == ApplicationState.READY_TO_APPLY
    assert jobs["gh-2"].application_state == ApplicationState.REVIEW_REQUIRED
    assert jobs["gh-3"].application_state == ApplicationState.SKIPPED_NO_SPONSORSHIP
    assert jobs["gh-4"].application_state == ApplicationState.SKIPPED_SENIORITY
    assert summary["jobs_fetched"] == 4
    assert summary["jobs_new"] == 4
    assert summary["hard_skips"] == 2
    assert summary["packages_generated"] == 2

    # Re-running the same cycle must not create duplicate application packages.
    summary2 = _run(monkeypatch, [CONFIRMED_REMOTE, LIKELY_SPONSOR, NO_SPONSORSHIP, OVERLY_SENIOR])
    assert len(list_jobs()) == 4
    assert summary2["jobs_new"] == 0
    assert summary2["jobs_deduplicated"] == 4


def test_provider_failure_does_not_abort_cycle(tmp_env, sample_profile, monkeypatch):
    save_profile(sample_profile)
    monkeypatch.setattr(
        cycle_mod, "get_enabled_providers",
        lambda: [FailingProvider(), FakeProvider([CONFIRMED_REMOTE])],
    )
    summary = cycle_mod.run_discovery_cycle()
    assert len(summary["errors"]) == 1
    assert "simulated provider outage" in summary["errors"][0]
    jobs = list_jobs()
    assert len(jobs) == 1
    assert jobs[0].application_state == ApplicationState.READY_TO_APPLY


def test_per_job_error_isolated(tmp_env, sample_profile, monkeypatch):
    save_profile(sample_profile)

    class ExplodingJob:
        provider = "fake"
        external_job_id = "boom"

    # A malformed raw job (missing attrs the cycle expects) must not prevent
    # the well-formed job in the same batch from being processed.
    monkeypatch.setattr(
        cycle_mod, "get_enabled_providers",
        lambda: [FakeProvider([ExplodingJob(), CONFIRMED_REMOTE])],
    )
    summary = cycle_mod.run_discovery_cycle()
    assert len(summary["errors"]) == 1
    jobs = list_jobs()
    assert len(jobs) == 1
    assert jobs[0].application_state == ApplicationState.READY_TO_APPLY
