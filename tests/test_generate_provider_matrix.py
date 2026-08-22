"""CLAUDE.md Phase 15 section 71: one authoritative, generated (never
hand-maintained) provider capability matrix."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import generate_provider_matrix  # noqa: E402


def test_merged_matrix_includes_every_discovery_provider():
    from app.providers.registry import all_capabilities

    rows = generate_provider_matrix.build_merged_matrix()
    row_names = {r["provider"] for r in rows}
    discovery_names = {c.provider_name for c in all_capabilities()}
    assert discovery_names <= row_names


def test_only_mock_ats_claims_auto_submit():
    rows = generate_provider_matrix.build_merged_matrix()
    auto_submit_providers = [r["provider"] for r in rows if r["auto_submit_supported"]]
    assert auto_submit_providers == ["mock_ats"]


def test_render_text_reports_auto_submit_truth():
    rows = generate_provider_matrix.build_merged_matrix()
    text = generate_provider_matrix.render_text(rows)
    assert "mock_ats" in text
    assert "auto-submit=True" in text
