"""Greenhouse Verified Submission Contract V1: real Chromium-driven E2E tests
for `app.applications.greenhouse_submit_engine`. Every posting is a local
`file://` fixture (tests/browser_fixtures.py) -- never a real website, never
a real employer. The fixture's own "Submit" button performs a `fetch()` to a
fixed, fake `https://greenhouse-fixture.local/apply` endpoint that Playwright
`page.route()` intercepts deterministically per scenario -- there is no real
network call anywhere in this file.

Marked `browser`: skipped automatically unless Playwright AND its Chromium
binary are actually launchable."""

import httpx
import pytest

from app import config
from app.applications import greenhouse_submit_claim as claim
from app.applications import provider_registry
from app.applications.greenhouse_submit_contract import SubmitOutcome
from app.applications.greenhouse_submit_engine import run_greenhouse_submit
from app.applications.providers_greenhouse import GreenhouseApplicationProvider
from app.candidate.profile import save_profile
from app.models import ApplicationMode, Job
from app.pipeline import ingest_and_process

from tests.test_greenhouse_submit_contract import MINIMAL_PAYLOAD, _drive_to_approved

pytestmark = pytest.mark.browser

JD_TEXT = (
    "We are hiring a Backend Software Engineer to build REST APIs in Python using FastAPI. "
    "This is a full-time position. H-1B sponsorship is available for this role."
)


@pytest.fixture(autouse=True)
def _require_chromium_launchable():
    playwright = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
    try:
        with playwright.sync_playwright() as p:
            p.chromium.launch(headless=True).close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"chromium browser binary not launchable: {exc}")


@pytest.fixture(autouse=True)
def _timeouts(monkeypatch):
    monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", True)
    monkeypatch.setattr(config, "BROWSER_ASSIST_TIMEOUT_SECONDS", 15)
    monkeypatch.setattr(config, "BROWSER_DOM_STABILIZATION_TIMEOUT_MS", 6000)
    monkeypatch.setattr(config, "BROWSER_DOM_STABILIZATION_POLL_MS", 100)
    monkeypatch.setattr(config, "GREENHOUSE_SUBMIT_CLICK_TIMEOUT_MS", 3000)
    # This module never enables the real canary -- every test exercises the
    # engine directly.
    monkeypatch.setattr(config, "GREENHOUSE_SUBMIT_CANARY_ENABLED", False)
    monkeypatch.setattr(config, "APPLICATION_EXECUTOR_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_SUBMIT_ENABLED", False)


@pytest.fixture(autouse=True)
def _mock_greenhouse_api():
    """Installed for the WHOLE test (setup AND the later run_greenhouse_submit
    call, which independently re-derives the provider via
    app.applications.provider_registry.get_application_provider) -- never
    just during job setup, or the contract's own re-checks inside the engine
    would silently fall through to a real network call."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=MINIMAL_PAYLOAD)

    original = provider_registry._PROVIDERS["greenhouse"]
    provider_registry._PROVIDERS["greenhouse"] = GreenhouseApplicationProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    yield
    provider_registry._PROVIDERS["greenhouse"] = original


def _approved_job(tmp_env, sample_profile, url: str, external_job_id: str) -> Job:
    """Drives a real job through the unmodified executor/approval pipeline
    (mocked Greenhouse Job Board API -- no live network) to a current, ACTIVE
    approval, with `canonical_url` set to a local file:// fixture BEFORE
    approval is recorded (so the approval's job-identity fingerprint already
    covers this exact URL)."""
    save_profile(sample_profile)
    job = ingest_and_process(Job(
        title="Backend Software Engineer", company="Acme Corp", location="Remote - US",
        description=JD_TEXT, employment_type="Full-time", provider="greenhouse",
        external_job_id=external_job_id, company_identifier="acme", mode=ApplicationMode.ASSIST,
        canonical_url=url, url=url,
    ))
    _drive_to_approved(job)
    return job


def _route_returning(status: int, body: str):
    def hook(page):
        page.route(
            "https://greenhouse-fixture.local/apply",
            lambda route: route.fulfill(status=status, content_type="text/html", body=body),
        )
    return hook


def _route_hanging():
    def hook(page):
        # Never call fulfill/continue_/abort -- the fetch stays pending
        # indefinitely, exactly the "timeout after a possible submit" case.
        page.route("https://greenhouse-fixture.local/apply", lambda route: None)
    return hook


def _route_connection_reset():
    def hook(page):
        page.route("https://greenhouse-fixture.local/apply", lambda route: route.abort("connectionreset"))
    return hook


def test_successful_submit_reaches_confirmed(tmp_env, sample_profile, tmp_path):
    from tests.browser_fixtures import greenhouse_like_submit_flow_page

    url = greenhouse_like_submit_flow_page(tmp_path)
    job = _approved_job(tmp_env, sample_profile, url, "gh-eng-success")
    monkeypatch_route = _route_returning(
        200, "<h1>Thank you for applying to Acme Corp</h1><p>Confirmation Number: GH-2026-99881</p>",
    )

    result = run_greenhouse_submit(job.id, headless=True, _test_route_hook=monkeypatch_route)

    assert result.outcome == SubmitOutcome.CONFIRMED, result.detail
    assert result.confirmation_id == "GH-2026-99881"

    from app.jobs_repo import get_job

    refreshed = get_job(job.id)
    assert refreshed.application_state.value == "APPLIED"

    from app.applications import receipts

    receipt_rows = receipts.list_receipts(provider="greenhouse")
    assert any(r["confirmation_id"] == "GH-2026-99881" for r in receipt_rows)


def test_server_validation_error_is_rejected(tmp_env, sample_profile, tmp_path):
    from tests.browser_fixtures import greenhouse_like_submit_flow_page

    url = greenhouse_like_submit_flow_page(tmp_path)
    job = _approved_job(tmp_env, sample_profile, url, "gh-eng-validation")
    hook = _route_returning(422, '<div class="error" role="alert">This field is required</div>')

    result = run_greenhouse_submit(job.id, headless=True, _test_route_hook=hook)

    assert result.outcome == SubmitOutcome.REJECTED
    assert "validation" in result.detail.lower() or "required" in result.detail.lower()


def test_duplicate_confirmation_handling_is_blocked_not_confirmed(tmp_env, sample_profile, tmp_path):
    from tests.browser_fixtures import greenhouse_like_submit_flow_page

    url = greenhouse_like_submit_flow_page(tmp_path)
    job = _approved_job(tmp_env, sample_profile, url, "gh-eng-duplicate")
    hook = _route_returning(200, "<p>You have already applied to this position.</p>")

    result = run_greenhouse_submit(job.id, headless=True, _test_route_hook=hook)

    assert result.outcome == SubmitOutcome.BLOCKED
    assert result.error_type == "DUPLICATE_APPLICATION_DETECTED"


def test_unrecognized_response_becomes_unknown(tmp_env, sample_profile, tmp_path):
    from tests.browser_fixtures import greenhouse_like_submit_flow_page

    url = greenhouse_like_submit_flow_page(tmp_path)
    job = _approved_job(tmp_env, sample_profile, url, "gh-eng-unknown")
    hook = _route_returning(200, "<p>Please wait while we process your request.</p>")

    result = run_greenhouse_submit(job.id, headless=True, _test_route_hook=hook)

    assert result.outcome == SubmitOutcome.SUBMISSION_STATUS_UNKNOWN
    assert result.error_type == "UNRECOGNIZED_OUTCOME"


def test_timeout_after_possible_submit_is_unknown_never_confirmed(tmp_env, sample_profile, tmp_path):
    from tests.browser_fixtures import greenhouse_like_submit_flow_page

    url = greenhouse_like_submit_flow_page(tmp_path)
    job = _approved_job(tmp_env, sample_profile, url, "gh-eng-timeout-after")

    result = run_greenhouse_submit(job.id, headless=True, _test_route_hook=_route_hanging())

    assert result.outcome == SubmitOutcome.SUBMISSION_STATUS_UNKNOWN
    assert result.error_type == "TIMEOUT_AFTER_CLICK"


def test_connection_loss_after_click_is_unknown_never_confirmed(tmp_env, sample_profile, tmp_path):
    from tests.browser_fixtures import greenhouse_like_submit_flow_page

    url = greenhouse_like_submit_flow_page(tmp_path)
    job = _approved_job(tmp_env, sample_profile, url, "gh-eng-connloss")

    result = run_greenhouse_submit(job.id, headless=True, _test_route_hook=_route_connection_reset())

    assert result.outcome == SubmitOutcome.SUBMISSION_STATUS_UNKNOWN
    assert result.error_type == "CONNECTION_LOST"


def test_timeout_before_submit_never_dispatches_a_click(tmp_env, sample_profile, tmp_path):
    """A permanently-disabled submit control -- Playwright's own click()
    actionability wait genuinely times out before any click is ever
    dispatched, distinct from a hung post-click fetch."""
    from tests.browser_fixtures import greenhouse_like_submit_flow_page

    url = greenhouse_like_submit_flow_page(tmp_path, disabled_submit=True)
    job = _approved_job(tmp_env, sample_profile, url, "gh-eng-timeout-before")

    result = run_greenhouse_submit(job.id, headless=True)

    assert result.outcome == SubmitOutcome.SUBMISSION_STATUS_UNKNOWN
    assert result.error_type == "TIMEOUT_BEFORE_CLICK"
    assert "no click was ever dispatched" in result.detail


def test_captcha_is_blocked_never_bypassed(tmp_env, sample_profile, tmp_path):
    from tests.browser_fixtures import greenhouse_like_captcha_page

    url = greenhouse_like_captcha_page(tmp_path)
    job = _approved_job(tmp_env, sample_profile, url, "gh-eng-captcha")

    result = run_greenhouse_submit(job.id, headless=True)

    assert result.outcome == SubmitOutcome.BLOCKED
    assert "CAPTCHA" in result.error_type


def test_login_wall_is_blocked_never_bypassed(tmp_env, sample_profile, tmp_path):
    from tests.browser_fixtures import greenhouse_like_login_page

    url = greenhouse_like_login_page(tmp_path)
    job = _approved_job(tmp_env, sample_profile, url, "gh-eng-login")

    result = run_greenhouse_submit(job.id, headless=True)

    assert result.outcome == SubmitOutcome.BLOCKED
    assert "LOGIN" in result.error_type


def test_expired_job_page_is_blocked(tmp_env, sample_profile, tmp_path):
    from tests.browser_fixtures import greenhouse_like_expired_page

    url = greenhouse_like_expired_page(tmp_path)
    job = _approved_job(tmp_env, sample_profile, url, "gh-eng-expired-page")

    result = run_greenhouse_submit(job.id, headless=True)

    assert result.outcome == SubmitOutcome.BLOCKED


def test_double_submit_is_refused_without_opening_a_second_browser(tmp_env, sample_profile, tmp_path):
    from tests.browser_fixtures import greenhouse_like_submit_flow_page

    url = greenhouse_like_submit_flow_page(tmp_path)
    job = _approved_job(tmp_env, sample_profile, url, "gh-eng-doubleclick")
    hook = _route_returning(
        200, "<h1>Thank you for applying to Acme Corp</h1><p>Confirmation Number: GH-2026-11223</p>",
    )

    first = run_greenhouse_submit(job.id, headless=True, _test_route_hook=hook)
    assert first.outcome == SubmitOutcome.CONFIRMED

    second = run_greenhouse_submit(job.id, headless=True, _test_route_hook=hook)
    assert second.outcome == SubmitOutcome.BLOCKED
    # The first call already left the execution APPLIED (terminal, no
    # longer "active"), so the second call's own contract lookup honestly
    # reports NO_EXECUTION -- an equally safe refusal as ALREADY_ATTEMPTED,
    # since no browser is opened and no second click is ever attempted
    # either way. The claim row's own state (checked below) is the actual
    # ground truth for "was a submit action ever attempted twice".
    assert second.error_type in ("ALREADY_ATTEMPTED", "NO_EXECUTION")

    # Exactly one attempted claim row for this execution.
    from app.applications import repo

    execution = repo.list_executions_for_job(job.id)[-1]
    claim_row = claim.get_claim(execution["execution_id"])
    assert claim_row["submit_attempted"] == 1
    assert claim_row["outcome"] == "CONFIRMED"


def test_submit_control_not_uniquely_identified_is_blocked(tmp_env, sample_profile, tmp_path, monkeypatch):
    """Two visible FINAL_SUBMIT-classified controls on the same page must
    never be resolved by guessing -- BLOCKED, never a click."""
    from tests.browser_fixtures import _write, _jsonld_block, _GREENHOUSE_STANDARD_FIELDS
    import textwrap

    url = _write(tmp_path, "greenhouse_two_submits.html", _jsonld_block() + textwrap.dedent(f"""
        <form id="application-form">
          {_GREENHOUSE_STANDARD_FIELDS}
          <button type="submit" id="submit-btn-1">Submit Application</button>
          <button type="submit" id="submit-btn-2">Submit Application</button>
        </form>
    """))
    job = _approved_job(tmp_env, sample_profile, url, "gh-eng-ambiguous")

    result = run_greenhouse_submit(job.id, headless=True)

    assert result.outcome == SubmitOutcome.BLOCKED
    assert result.error_type == "SUBMIT_CONTROL_NOT_UNIQUE"

    from app.applications import repo

    execution = repo.get_active_execution_for_job(job.id)
    assert claim.already_attempted(execution["execution_id"]) is False


# --- Multi-signal confirmation contract (2026-09-04, job 454/Anthropic's
# real canary returned UNRECOGNIZED_OUTCOME) --------------------------------

def test_previously_unrecognized_wording_now_reaches_confirmed(tmp_env, sample_profile, tmp_path):
    """A real-world ATS confirmation wording this project had not yet
    curated (job 454's own real failure mode: an ambiguous/unrecognized
    response) -- now correctly reaches CONFIRMED via the broadened phrase
    table, exercised through the actual production engine end-to-end, not
    just the parser in isolation."""
    from tests.browser_fixtures import greenhouse_like_submit_flow_page

    url = greenhouse_like_submit_flow_page(tmp_path)
    job = _approved_job(tmp_env, sample_profile, url, "gh-eng-new-phrase")
    hook = _route_returning(200, "<h1>You're all set!</h1><p>We'll be in touch soon.</p>")

    result = run_greenhouse_submit(job.id, headless=True, _test_route_hook=hook)

    assert result.outcome == SubmitOutcome.CONFIRMED, result.detail


def test_structural_disappearance_corroborates_a_moderate_phrase_to_strong(tmp_env, sample_profile, tmp_path):
    """The clicked submit control and the rest of the form both genuinely
    disappearing (the real fixture's own post-submit DOM replacement) is
    fed through as structural corroboration -- a real, end-to-end proof
    that config.BROWSER_DOM_STABILIZATION_* structural signals reach the
    confirmation grade, not just a unit-tested code path."""
    from app.applications import confirmation_evidence as _ce
    from tests.browser_fixtures import greenhouse_like_submit_flow_page

    captured = {}
    original = _ce.classify_confirmation_evidence

    def _spy(**kwargs):
        captured.update(kwargs)
        return original(**kwargs)

    import app.applications.greenhouse_submit_engine as engine_module
    engine_module.classify_confirmation_evidence = _spy
    try:
        url = greenhouse_like_submit_flow_page(tmp_path)
        job = _approved_job(tmp_env, sample_profile, url, "gh-eng-structural")
        hook = _route_returning(200, "<h1>Thank you for applying to Acme Corp</h1>")

        result = run_greenhouse_submit(job.id, headless=True, _test_route_hook=hook)
    finally:
        engine_module.classify_confirmation_evidence = original

    assert result.outcome == SubmitOutcome.CONFIRMED, result.detail
    # The real fixture's post-submit DOM genuinely removes the form/submit
    # control -- both signals must have reached the grader as True, not None.
    assert captured.get("submit_control_disappeared") is True
    assert captured.get("form_fields_disappeared") is True


# --- Greenhouse Confirmation Detection Forensics V1 (2026-09-04): jobs
# 454/291/342's real canary attempts all reached SUBMISSION_STATUS_UNKNOWN/
# UNRECOGNIZED_OUTCOME with zero durable evidence of what was actually
# observed. Two real, provable gaps this closes: (1) body/heading text was
# sampled the INSTANT the old submit control disappeared, with no settle
# time for a genuinely async-rendered replacement; (2) no evidence beyond a
# generic detail string was ever persisted on a non-CONFIRMED outcome. ------

def test_delayed_confirmation_render_is_still_caught_by_the_settle_wait(tmp_env, sample_profile, tmp_path):
    """Real, provable timing gap: the old submit control is removed from the
    DOM THE INSTANT the fetch resolves (so control-disappearance detection
    fires immediately), but the actual confirmation text only renders
    150ms LATER via setTimeout -- exactly the SPA race a genuinely
    async-rendered confirmation page can produce. Without a post-click
    settle wait, body_text would be sampled against the still-empty
    intermediate DOM and this would incorrectly reach UNRECOGNIZED_OUTCOME.
    150ms is deliberately well under this test module's overridden 3-poll
    x 100ms (=300ms) premature-stability window, so the settle wait is
    still genuinely waiting (not yet mid-consecutive-stable-count) when the
    real content lands."""
    from tests.browser_fixtures import greenhouse_like_submit_flow_page

    url = greenhouse_like_submit_flow_page(tmp_path, delayed_confirmation_ms=150)
    job = _approved_job(tmp_env, sample_profile, url, "gh-eng-delayed-confirm")
    hook = _route_returning(200, "<h1>Thank you for applying to Acme Corp</h1><p>Confirmation Number: GH-DELAY-1</p>")

    result = run_greenhouse_submit(job.id, headless=True, _test_route_hook=hook)

    assert result.outcome == SubmitOutcome.CONFIRMED, result.detail
    assert result.confirmation_id == "GH-DELAY-1"


def test_unrecognized_outcome_persists_evidence_for_future_diagnosis(tmp_env, sample_profile, tmp_path):
    """The exact known structural shape of jobs 454/291/342's real failures:
    the page genuinely changed (form/control both gone) but the text
    matches no curated phrase anywhere. Per the task's own rule, a
    historical case with insufficient captured evidence must stay UNKNOWN,
    never be inflated to a success -- and this proves the NEW evidence
    columns are actually populated now, closing the observability gap that
    made a genuine post-hoc diagnosis of 454/291/342 impossible."""
    from tests.browser_fixtures import greenhouse_like_submit_flow_page

    url = greenhouse_like_submit_flow_page(tmp_path)
    job = _approved_job(tmp_env, sample_profile, url, "gh-eng-evidence-capture")
    hook = _route_returning(200, "<h1>Please wait</h1><p>We are processing your request.</p>")

    result = run_greenhouse_submit(job.id, headless=True, _test_route_hook=hook)

    assert result.outcome == SubmitOutcome.SUBMISSION_STATUS_UNKNOWN
    assert result.error_type == "UNRECOGNIZED_OUTCOME"

    row = claim.get_claim(_find_execution_id(job.id))
    assert row is not None
    assert row["submit_attempted"] == 1
    assert row["final_url"].startswith("file://")
    assert "please wait" in row["heading_text"].lower()
    assert "processing your request" in row["body_text_snippet"].lower()
    assert row["phrase_matched"] == 0
    assert row["heading_phrase_matched"] == 0
    # The fixture's post-submit DOM genuinely removes the form/control --
    # both structural signals must be captured too, not left NULL.
    assert row["submit_control_disappeared"] == 1
    assert row["form_fields_disappeared"] == 1


def test_blank_response_never_falsely_confirms(tmp_env, sample_profile, tmp_path):
    """Adversarial: a genuinely blank/partial post-submit page -- no text
    at all -- must never be treated as a success. False-positive success is
    worse than an honest UNKNOWN."""
    from tests.browser_fixtures import greenhouse_like_submit_flow_page

    url = greenhouse_like_submit_flow_page(tmp_path)
    job = _approved_job(tmp_env, sample_profile, url, "gh-eng-blank")
    hook = _route_returning(200, "")

    result = run_greenhouse_submit(job.id, headless=True, _test_route_hook=hook)

    assert result.outcome == SubmitOutcome.SUBMISSION_STATUS_UNKNOWN
    assert result.error_type == "UNRECOGNIZED_OUTCOME"


def test_negative_application_wording_never_falsely_confirms(tmp_env, sample_profile, tmp_path):
    """Adversarial word-overlap trap: the resulting text contains
    'application' (the same noun a real success phrase also contains) but
    is actually a NEGATIVE/incomplete outcome -- must never be confused for
    a genuine success. Proves the curated phrase table's 'affirmative,
    completed-action phrase only' design (never a bare noun match) holds
    end-to-end through the real engine, not just at the parser-unit level."""
    from tests.browser_fixtures import greenhouse_like_submit_flow_page

    url = greenhouse_like_submit_flow_page(tmp_path)
    job = _approved_job(tmp_env, sample_profile, url, "gh-eng-negative-application")
    hook = _route_returning(
        200, "<h1>Something went wrong</h1><p>Your application was not submitted. Please try again later.</p>",
    )

    result = run_greenhouse_submit(job.id, headless=True, _test_route_hook=hook)

    assert result.outcome == SubmitOutcome.SUBMISSION_STATUS_UNKNOWN
    assert result.error_type == "UNRECOGNIZED_OUTCOME"


def _find_execution_id(job_id: int) -> str:
    """A terminal execution (APPLIED/SUBMISSION_STATUS_UNKNOWN/etc.) is no
    longer "active" by the time a test's assertions run -- a direct query
    by job_id is the same lookup every other test in this file already
    relies on implicitly via `run_greenhouse_submit`'s own internals."""
    from app.db import db_session

    with db_session() as conn:
        row = conn.execute(
            "SELECT execution_id FROM application_executions WHERE job_id = ? ORDER BY id DESC LIMIT 1",
            (job_id,),
        ).fetchone()
    return row["execution_id"] if row else ""
