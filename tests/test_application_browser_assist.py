"""CLAUDE.md Phase 9 sections 21-23, 53: browser-assist boundaries. Marked
`browser` -- skipped automatically unless Playwright AND its chromium
binary are installed (`pip install playwright && playwright install
chromium`), and never runs by default (`pytest -m browser` to opt in).
Serves local static HTML fixtures via `file://` -- never touches a real
website, never requires internet access."""

import json
import textwrap

import pytest

from app import config

pytestmark = pytest.mark.browser


@pytest.fixture(autouse=True)
def _require_playwright_installed():
    playwright = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
    try:
        with playwright.sync_playwright() as p:
            p.chromium.launch(headless=True).close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"chromium browser binary not installed: {exc}")


@pytest.fixture(autouse=True)
def _enable_browser_assist(monkeypatch):
    monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", True)
    monkeypatch.setattr(config, "BROWSER_ASSIST_HEADLESS", True)


def _write_fixture_page(tmp_path, body: str) -> str:
    path = tmp_path / "form.html"
    path.write_text(f"<html><body>{body}</body></html>")
    return path.as_uri()


def _fields():
    from app.applications.models import ApplicationField, FieldCategory, FieldConfidence

    return [
        ApplicationField(
            field_id="full_name", label="Full Name", category=FieldCategory.CONTACT, normalized_type="text",
            required=True, verified_value="Test Candidate", confidence=FieldConfidence.EXACT,
            auto_fill_allowed=True,
        ),
        ApplicationField(
            field_id="email", label="Email", category=FieldCategory.CONTACT, normalized_type="text",
            required=True, verified_value="test.candidate@example.com", confidence=FieldConfidence.EXACT,
            auto_fill_allowed=True,
        ),
        ApplicationField(
            field_id="veteran_status", label="Veteran Status", category=FieldCategory.DEMOGRAPHICS,
            normalized_type="select", verified_value="I am not a veteran", confidence=FieldConfidence.HIGH,
            auto_fill_allowed=True,
        ),
    ]


def test_fills_non_sensitive_verified_fields_and_stops_before_submit(tmp_path):
    from app.applications.browser_assist import prepare_application
    from app.models import Job

    url = _write_fixture_page(tmp_path, textwrap.dedent("""
        <form>
          <label for="fname">Full Name</label><input id="fname" name="full_name" type="text">
          <label for="mail">Email</label><input id="mail" name="email" type="text">
          <button type="submit">Submit Application</button>
        </form>
    """))
    job = Job(id=1, title="T", company="Acme", description="D", url=url, canonical_url=url)

    record = prepare_application(job, _fields())
    assert record.stage == "DRAFT_READY"
    assert "full_name" in record.prepared_field_ids
    assert "email" in record.prepared_field_ids
    assert record.unresolved_field_ids == []


def test_never_fills_a_demographic_field(tmp_path):
    """CLAUDE.md Phase 9 section 22/11: demographic/sensitive fields must
    never be auto-filled by the browser layer, even when a verified value
    exists in the candidate profile."""
    from app.applications.browser_assist import prepare_application
    from app.models import Job

    url = _write_fixture_page(tmp_path, textwrap.dedent("""
        <form>
          <label for="fname">Full Name</label><input id="fname" name="full_name" type="text">
          <label for="vet">Veteran Status</label><select id="vet" name="veteran_status">
            <option>I am a veteran</option><option>I am not a veteran</option>
          </select>
        </form>
    """))
    job = Job(id=2, title="T", company="Acme", description="D", url=url, canonical_url=url)

    record = prepare_application(job, _fields())
    assert "veteran_status" not in record.prepared_field_ids


def test_stops_at_login_wall(tmp_path):
    from app.applications.browser_assist import prepare_application
    from app.models import Job

    url = _write_fixture_page(tmp_path, textwrap.dedent("""
        <form>
          <label for="u">Username</label><input id="u" name="username" type="text">
          <label for="p">Password</label><input id="p" name="password" type="password">
        </form>
    """))
    job = Job(id=3, title="T", company="Acme", description="D", url=url, canonical_url=url)

    record = prepare_application(job, _fields())
    assert record.stage == "USER_ACTION_REQUIRED"
    assert record.reason == "LOGIN_REQUIRED"


def test_raises_when_disabled(monkeypatch, tmp_path):
    from app.applications.browser_assist import BrowserAssistUnavailable, prepare_application
    from app.models import Job

    monkeypatch.setattr(config, "BROWSER_ASSIST_ENABLED", False)
    url = _write_fixture_page(tmp_path, "<form></form>")
    job = Job(id=4, title="T", company="Acme", description="D", url=url, canonical_url=url)

    with pytest.raises(BrowserAssistUnavailable):
        prepare_application(job, _fields())
