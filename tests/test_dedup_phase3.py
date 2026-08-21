from datetime import datetime, timezone

from app.agent import cycle as cycle_mod
from app.candidate.profile import save_profile
from app.discovery.dedup import canonicalize_url, fingerprint
from app.jobs_repo import list_jobs, list_provenance
from app.models import ApplicationState, FreshnessSource
from app.providers.base import JobProvider, RawJobPosting


class FakeProvider(JobProvider):
    name = "fake"

    def __init__(self, jobs: list[RawJobPosting]):
        self._jobs = jobs

    def fetch_jobs(self, max_jobs: int) -> list[RawJobPosting]:
        return self._jobs[:max_jobs]


NOW = datetime.now(timezone.utc).isoformat()


def _confirmed(**overrides) -> RawJobPosting:
    defaults = dict(
        provider="fake", external_job_id="1", title="Backend Software Engineer",
        company="Acme Corp", location="Remote (US)",
        description=(
            "We are hiring a Backend Software Engineer to build REST APIs in Python "
            "using FastAPI and PostgreSQL. Fully remote. Visa sponsorship available."
        ),
        url="https://boards.greenhouse.io/acme/jobs/1?utm_source=linkedin&gh_jid=1",
        published_at=NOW,
    )
    defaults.update(overrides)
    return RawJobPosting(**defaults)


# --- URL canonicalization ----------------------------------------------------

def test_canonicalize_strips_tracking_params_but_keeps_job_id():
    url = "https://Boards.Greenhouse.io/acme/jobs/12345?utm_source=linkedin&utm_campaign=x&gh_jid=12345"
    canonical = canonicalize_url(url)
    assert "utm_source" not in canonical
    assert "utm_campaign" not in canonical
    assert "gh_jid=12345" in canonical
    assert canonical.startswith("https://boards.greenhouse.io")  # host lowercased


def test_canonicalize_strips_trailing_slash():
    assert canonicalize_url("https://acme.com/jobs/1/") == canonicalize_url("https://acme.com/jobs/1")


def test_canonicalize_is_stable_regardless_of_param_order():
    a = canonicalize_url("https://acme.com/jobs/1?b=2&a=1")
    b = canonicalize_url("https://acme.com/jobs/1?a=1&b=2")
    assert a == b


def test_canonicalize_empty_url_returns_empty_string():
    assert canonicalize_url("") == ""
    assert canonicalize_url("not-a-url") == ""


def test_fingerprint_unaffected_by_canonicalization_changes():
    fp1 = fingerprint("Acme Corp", "Backend Engineer", "Remote")
    fp2 = fingerprint("acme corp", "BACKEND ENGINEER", "remote")
    assert fp1 == fp2


# --- Acceptance scenario D: same requisition, two sources, one canonical job,
# two provenance records. --------------------------------------------------

def test_same_requisition_from_two_providers_dedupes_to_one_job_two_provenance(tmp_env, sample_profile, monkeypatch):
    save_profile(sample_profile)

    same_apply_url = "https://boards.greenhouse.io/acme/jobs/999"
    from_greenhouse = _confirmed(provider="greenhouse", external_job_id="999", url=same_apply_url + "?utm_source=x")
    from_syndicated_feed = _confirmed(
        provider="syndicated", external_job_id="different-id-from-other-source",
        url=same_apply_url + "?utm_source=indeed&gh_src=y",
    )

    monkeypatch.setattr(cycle_mod, "get_enabled_providers", lambda: [FakeProvider([from_greenhouse])])
    cycle_mod.run_discovery_cycle()
    monkeypatch.setattr(cycle_mod, "get_enabled_providers", lambda: [FakeProvider([from_syndicated_feed])])
    cycle_mod.run_discovery_cycle()

    jobs = list_jobs()
    assert len(jobs) == 1  # one canonical job, not two

    provenance = list_provenance(jobs[0].id)
    assert len(provenance) == 2
    providers_seen = {p["provider"] for p in provenance}
    assert providers_seen == {"greenhouse", "syndicated"}


def test_different_requisitions_same_company_title_location_not_wrongly_merged_when_stable_ids_differ(
    tmp_env, sample_profile, monkeypatch,
):
    """Two genuinely different postings must not merge just because
    title/company/location happen to match -- distinct provider IDs AND
    distinct canonical URLs must stay distinct jobs."""
    save_profile(sample_profile)
    job_a = _confirmed(provider="greenhouse", external_job_id="A",
                        url="https://boards.greenhouse.io/acme/jobs/aaa")
    job_b = _confirmed(provider="greenhouse", external_job_id="B",
                        url="https://boards.greenhouse.io/acme/jobs/bbb")

    monkeypatch.setattr(cycle_mod, "get_enabled_providers", lambda: [FakeProvider([job_a, job_b])])
    cycle_mod.run_discovery_cycle()

    jobs = list_jobs()
    assert len(jobs) == 2


# --- Acceptance scenario F: no published time -> FIRST_SEEN -----------------

def test_freshness_source_is_published_at_when_provider_gives_one(tmp_env, sample_profile, monkeypatch):
    save_profile(sample_profile)
    monkeypatch.setattr(cycle_mod, "get_enabled_providers", lambda: [FakeProvider([_confirmed()])])
    cycle_mod.run_discovery_cycle()
    job = list_jobs()[0]
    assert job.freshness_source == FreshnessSource.PUBLISHED_AT


def test_freshness_source_falls_back_to_first_seen_when_no_published_at(tmp_env, sample_profile, monkeypatch):
    save_profile(sample_profile)
    raw = _confirmed(published_at=None, url="https://boards.greenhouse.io/acme/jobs/no-date")
    monkeypatch.setattr(cycle_mod, "get_enabled_providers", lambda: [FakeProvider([raw])])
    cycle_mod.run_discovery_cycle()
    job = list_jobs()[0]
    assert job.freshness_source == FreshnessSource.FIRST_SEEN
    assert job.published_at is None
