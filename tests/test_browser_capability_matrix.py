"""CLAUDE.md Phase 10 sections 26-27, 59: browser-assist capability matrix
(separate from the Phase 8 application-provider capability matrix)."""

from fastapi.testclient import TestClient

from app.agent import state as agent_state
from app.applications.browser_capability_matrix import BrowserVerification, all_rows, render_text
from app.applications.cli import main as cli_main
from app.main import app


def test_all_rows_have_a_valid_verification_value():
    rows = all_rows()
    assert len(rows) > 0
    valid = {v.value for v in BrowserVerification}
    for row in rows:
        assert row["verification"] in valid


def test_greenhouse_lever_ashby_are_live_verified():
    """These three were genuinely opened against real live postings this
    phase -- see scripts/phase10_live_validation.py and
    docs/real-ats-validation.md."""
    rows = {r["provider"]: r for r in all_rows()}
    for provider in ("greenhouse", "lever", "ashby"):
        assert rows[provider]["verification"] == "LIVE_FORM_VERIFIED"
        assert rows[provider]["field_discovery"] is True


def test_no_provider_claims_final_submit_automation():
    """CLAUDE.md Phase 10 section 29: browser assist never clicks a final
    submit action for any real provider."""
    for row in all_rows():
        assert row["final_submit_automation"] is False


def test_render_text_produces_output():
    text = render_text()
    assert "greenhouse" in text
    assert "LIVE_FORM_VERIFIED" in text


def test_cli_browser_capability_matrix(tmp_env, capsys):
    rc = cli_main(["browser-capability-matrix"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Browser-Assist Capability Matrix" in out


def test_dashboard_page_loads(tmp_env, sample_profile):
    agent_state.set_enabled(False)
    from app.candidate.profile import save_profile

    save_profile(sample_profile)
    client = TestClient(app)
    resp = client.get("/applications/browser-capability-matrix")
    assert resp.status_code == 200
    assert "LIVE_FORM_VERIFIED" in resp.text
