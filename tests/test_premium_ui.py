"""Premium UI (feat/premium-product-ui branch): navigation, the Jobs
browser, Tracker board, Activity feed, read-only Profile page, the
Settings page's real save/validate round trip, the Applications page's
new tabs (including the synthetic "Skipped" tab), and the job detail
page's state-aware "Update state" control. Every route/button exercised
here must be reachable through app.main -- no client-side-only behavior
is asserted (that's covered by the Playwright suite, where available)."""

from fastapi.testclient import TestClient

from app import config
from app.candidate.profile import save_profile
from app.main import app
from app.models import ApplicationMode, ApplicationState, Job
from app.pipeline import ingest_and_process


def _confirmed_remote_job(**overrides) -> Job:
    defaults = dict(
        title="Backend Software Engineer",
        company="Acme Corp",
        location="Remote (US)",
        description=(
            "We are hiring a Backend Software Engineer to build REST APIs in Python "
            "using FastAPI and PostgreSQL. Fully remote. Visa sponsorship available. "
            "Required: Python, FastAPI, PostgreSQL, Docker."
        ),
        mode=ApplicationMode.ASSIST,
    )
    defaults.update(overrides)
    return Job(**defaults)


def _no_sponsorship_job(**overrides) -> Job:
    defaults = dict(
        title="Backend Software Engineer",
        company="NoSponsor Inc",
        location="Remote (US)",
        description="Backend role. We are not able to sponsor visas now or in the future.",
        mode=ApplicationMode.ASSIST,
    )
    defaults.update(overrides)
    return Job(**defaults)


# --- Navigation --------------------------------------------------------------

def test_primary_nav_present_on_dashboard(tmp_env, sample_profile):
    save_profile(sample_profile)
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    for href in ("/jobs", "/applications", "/tracker", "/activity", "/profile", "/settings"):
        assert f'href="{href}"' in resp.text


def test_static_assets_served(tmp_env):
    client = TestClient(app)
    css = client.get("/static/css/app.css")
    assert css.status_code == 200
    assert "topbar" in css.text
    js = client.get("/static/js/app.js")
    assert js.status_code == 200
    assert "pollAgentStatus" in js.text


# --- Jobs page -----------------------------------------------------------

def test_jobs_page_loads_and_lists_ingested_job(tmp_env, sample_profile):
    save_profile(sample_profile)
    client = TestClient(app)
    job = ingest_and_process(_confirmed_remote_job())
    resp = client.get("/jobs")
    assert resp.status_code == 200
    assert job.company in resp.text
    assert f'href="/jobs/{job.id}"' in resp.text


def test_jobs_page_search_filters_by_company(tmp_env, sample_profile):
    save_profile(sample_profile)
    client = TestClient(app)
    ingest_and_process(_confirmed_remote_job(company="Acme Corp"))
    ingest_and_process(_confirmed_remote_job(company="Zenith Systems", external_job_id="z1"))

    resp = client.get("/jobs", params={"q": "Zenith"})
    assert resp.status_code == 200
    assert "Zenith Systems" in resp.text
    assert "jc-company\">Acme Corp" not in resp.text


def test_jobs_page_add_job_form_ingests(tmp_env, sample_profile):
    save_profile(sample_profile)
    client = TestClient(app)
    resp = client.get("/jobs")
    assert 'action="/jobs/ingest"' in resp.text

    ingest_resp = client.post("/jobs/ingest", data={
        "title": "Platform Engineer", "company": "New Co", "location": "Remote",
        "description": "Platform engineering role. Sponsorship available.", "mode": "ASSIST",
    }, follow_redirects=False)
    assert ingest_resp.status_code == 303

    resp2 = client.get("/jobs", params={"q": "New Co"})
    assert "New Co" in resp2.text


# --- Tracker ---------------------------------------------------------------

def test_tracker_page_loads_with_all_lanes(tmp_env, sample_profile):
    save_profile(sample_profile)
    client = TestClient(app)
    resp = client.get("/tracker")
    assert resp.status_code == 200
    for label in ("Applied", "Assessment", "Interview", "Offer", "Rejected", "Withdrawn"):
        assert label in resp.text


def test_tracker_reflects_manual_state_progression(tmp_env, sample_profile):
    save_profile(sample_profile)
    client = TestClient(app)
    job = ingest_and_process(_confirmed_remote_job())
    assert job.application_state == ApplicationState.READY_TO_APPLY

    for target in ("APPLIED", "ASSESSMENT", "INTERVIEW", "OFFER"):
        resp = client.post(f"/jobs/{job.id}/state", data={"target_state": target}, follow_redirects=False)
        assert resp.status_code == 303, f"transition to {target} failed"

    tracker_resp = client.get("/tracker")
    assert tracker_resp.status_code == 200
    assert job.company in tracker_resp.text

    from app.jobs_repo import get_job
    assert get_job(job.id).application_state == ApplicationState.OFFER


def test_job_detail_update_state_only_offers_valid_transitions(tmp_env, sample_profile):
    save_profile(sample_profile)
    client = TestClient(app)
    job = ingest_and_process(_confirmed_remote_job())
    assert job.application_state == ApplicationState.READY_TO_APPLY

    resp = client.get(f"/jobs/{job.id}")
    assert resp.status_code == 200
    # Legal from READY_TO_APPLY:
    assert 'value="APPLIED"' in resp.text
    assert 'value="SKIPPED"' in resp.text
    # Not legal directly from READY_TO_APPLY (OFFER/ASSESSMENT require APPLIED first):
    assert 'value="OFFER"' not in resp.text
    assert 'value="ASSESSMENT"' not in resp.text


def test_invalid_manual_transition_rejected(tmp_env, sample_profile):
    save_profile(sample_profile)
    client = TestClient(app)
    job = ingest_and_process(_confirmed_remote_job())
    # READY_TO_APPLY -> OFFER is not a legal manual transition.
    resp = client.post(f"/jobs/{job.id}/state", data={"target_state": "OFFER"})
    assert resp.status_code == 400


# --- Activity ----------------------------------------------------------------

def test_activity_page_loads(tmp_env, sample_profile):
    save_profile(sample_profile)
    client = TestClient(app)
    ingest_and_process(_confirmed_remote_job())
    resp = client.get("/activity")
    assert resp.status_code == 200
    assert "Live Activity" in resp.text


def test_activity_page_shows_needs_action_item(tmp_env, sample_profile):
    save_profile(sample_profile)
    client = TestClient(app)
    job = ingest_and_process(_confirmed_remote_job(
        description="Join our backend Python team building APIs.",
    ))
    assert job.application_state == ApplicationState.REVIEW_REQUIRED
    resp = client.get("/activity")
    assert resp.status_code == 200
    assert "Needs Your Action" in resp.text
    assert job.company in resp.text


# --- Profile -----------------------------------------------------------------

def test_profile_page_shows_filled_fields(tmp_env, sample_profile):
    save_profile(sample_profile)
    client = TestClient(app)
    resp = client.get("/profile")
    assert resp.status_code == 200
    assert sample_profile.contact.full_name in resp.text


def test_profile_page_flags_missing_fields(tmp_env):
    from app.candidate.schema import CandidateProfile

    save_profile(CandidateProfile())
    client = TestClient(app)
    resp = client.get("/profile")
    assert resp.status_code == 200
    assert "needs input" in resp.text


# --- Settings ------------------------------------------------------------

def test_settings_page_shows_current_values(tmp_env):
    client = TestClient(app)
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "Agent poll interval" in resp.text
    assert "Max jobs fetched per cycle" in resp.text


def test_settings_save_persists_and_applies_live(tmp_env, monkeypatch):
    # save_settings() genuinely mutates the shared, process-global app.config
    # module by design (see app/settings_store.py) -- snapshot-and-restore via
    # monkeypatch so this test (which exercises that real live-apply behavior
    # through the actual endpoint) can never leak AGENT_INTERVAL_MINUTES/
    # MAX_JOBS_PER_CYCLE/FRESHNESS_MAX_DAYS into other tests in the same
    # pytest session regardless of run order.
    monkeypatch.setattr(config, "AGENT_INTERVAL_MINUTES", config.AGENT_INTERVAL_MINUTES)
    monkeypatch.setattr(config, "MAX_JOBS_PER_CYCLE", config.MAX_JOBS_PER_CYCLE)
    monkeypatch.setattr(config, "FRESHNESS_MAX_DAYS", config.FRESHNESS_MAX_DAYS)
    client = TestClient(app)
    resp = client.post("/settings", data={
        "agent_interval_minutes": "9", "max_jobs_per_cycle": "17", "freshness_max_days": "4",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings?saved=true"

    assert config.AGENT_INTERVAL_MINUTES == 9
    assert config.MAX_JOBS_PER_CYCLE == 17
    assert config.FRESHNESS_MAX_DAYS == 4

    from app.settings_store import load_overrides
    stored = load_overrides()
    assert stored["agent_interval_minutes"] == 9

    confirm_resp = client.get("/settings?saved=true")
    assert "Settings saved." in confirm_resp.text
    assert 'value="9"' in confirm_resp.text


def test_settings_save_rejects_out_of_range_value(tmp_env, monkeypatch):
    # Only agent_interval_minutes is invalid here -- save_settings() still
    # applies the OTHER valid keys in the same request (partial apply, see
    # app/settings_store.py's docstring), so this test leaks
    # MAX_JOBS_PER_CYCLE/FRESHNESS_MAX_DAYS into global config just like
    # test_settings_save_persists_and_applies_live above unless restored.
    monkeypatch.setattr(config, "AGENT_INTERVAL_MINUTES", config.AGENT_INTERVAL_MINUTES)
    monkeypatch.setattr(config, "MAX_JOBS_PER_CYCLE", config.MAX_JOBS_PER_CYCLE)
    monkeypatch.setattr(config, "FRESHNESS_MAX_DAYS", config.FRESHNESS_MAX_DAYS)
    client = TestClient(app)
    before = config.AGENT_INTERVAL_MINUTES
    resp = client.post("/settings", data={
        "agent_interval_minutes": "999999", "max_jobs_per_cycle": "17", "freshness_max_days": "4",
    })
    assert resp.status_code == 400
    assert "must be between" in resp.text
    # Rejected setting never applied -- not even a partial/best-effort save.
    assert config.AGENT_INTERVAL_MINUTES == before


def test_settings_never_exposes_auto_submit_toggle(tmp_env):
    """CLAUDE.md's 'never silently enable' rules -- auto-submit/executor/
    browser-assist stay env-only, no <input name="auto_submit_enabled">
    or similar mutable control anywhere on the page."""
    client = TestClient(app)
    resp = client.get("/settings")
    assert 'name="auto_submit_enabled"' not in resp.text
    assert 'name="application_executor_enabled"' not in resp.text
    assert "Auto-submit" in resp.text  # still shown, just read-only


# --- Applications page tabs --------------------------------------------------

def test_applications_page_shows_tabs_with_counts(tmp_env, sample_profile):
    save_profile(sample_profile)
    client = TestClient(app)
    resp = client.get("/applications")
    assert resp.status_code == 200
    for label in ("All", "Ready to Apply", "Needs Action", "Applying", "Applied", "Failed", "Skipped"):
        assert label in resp.text


def test_applications_skipped_tab_shows_skipped_job(tmp_env, sample_profile):
    save_profile(sample_profile)
    client = TestClient(app)
    job = ingest_and_process(_no_sponsorship_job())
    assert job.application_state.value.startswith("SKIPPED")

    resp = client.get("/applications", params={"bucket": "skipped"})
    assert resp.status_code == 200
    assert job.company in resp.text


# --- Live/API aggregation -----------------------------------------------

def test_api_dashboard_live_endpoint(tmp_env, sample_profile):
    save_profile(sample_profile)
    client = TestClient(app)
    ingest_and_process(_confirmed_remote_job())
    resp = client.get("/api/dashboard/live")
    assert resp.status_code == 200
    body = resp.json()
    for key in ("agent", "orchestrator", "summary", "needs_action_queue", "recent_activity"):
        assert key in body
    assert "fresh_jobs" in body["summary"]
