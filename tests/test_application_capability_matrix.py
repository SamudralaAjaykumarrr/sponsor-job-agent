"""CLAUDE.md Phase 9 section 44: truthful provider capability matrix."""

from app.applications.capability_matrix import build_matrix, render_text


def test_matrix_includes_every_registered_provider(tmp_env):
    matrix = build_matrix()
    providers = {r["provider"] for r in matrix["rows"]}
    assert {"mock_ats", "greenhouse", "lever", "generic"} <= providers


def test_only_mock_ats_declares_submission_supported(tmp_env):
    matrix = build_matrix()
    submission_capable = {r["provider"] for r in matrix["rows"] if r["submission_supported"]}
    assert submission_capable == {"mock_ats"}


def test_only_mock_ats_declares_confirmation_recheck_supported(tmp_env):
    matrix = build_matrix()
    recheck_capable = {r["provider"] for r in matrix["rows"] if r["confirmation_recheck_supported"]}
    assert recheck_capable == {"mock_ats"}


def test_render_text_is_nonempty_and_mentions_every_provider(tmp_env):
    text = render_text()
    for provider in ("mock_ats", "greenhouse", "lever", "generic"):
        assert provider in text
