"""CLAUDE.md Phase 15 section 26: deterministic local secret scanner."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import secret_scan  # noqa: E402


def test_current_repo_tracked_files_are_clean():
    """The actual repository must currently pass the scan -- this is a real
    regression guard, not just a unit test of the pattern logic."""
    findings = secret_scan.scan(secret_scan._tracked_files())
    assert findings == []


def test_detects_aws_access_key():
    text = 'AWS_KEY = "AKIAABCDEFGHIJKLMNOP"\n'
    assert any(p.search(text) for _, p in secret_scan.CONTENT_PATTERNS)


def test_detects_private_key_block(tmp_path):
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIB...\n-----END RSA PRIVATE KEY-----\n"
    assert any(p.search(text) for _, p in secret_scan.CONTENT_PATTERNS)


def test_does_not_flag_placeholder_connection_string():
    text = "DATABASE_URL=postgresql://user:password@host:port/dbname"
    matches = [label for label, p in secret_scan.CONTENT_PATTERNS if p.search(text)]
    assert matches == []


def test_flags_real_looking_connection_string():
    text = "DATABASE_URL=postgresql://sponsor_job_agent:xK9$pQzR2m@db.internal:5432/prod"
    matches = [label for label, p in secret_scan.CONTENT_PATTERNS if p.search(text)]
    assert "connection string with credentials" in matches


def test_env_example_path_not_flagged_as_forbidden():
    findings = secret_scan.scan([".env.example"])
    assert not any("FORBIDDEN PATH" in f for f in findings)


def test_real_env_file_path_flagged_as_forbidden(tmp_path, monkeypatch):
    monkeypatch.setattr(secret_scan, "REPO_ROOT", tmp_path)
    (tmp_path / ".env").write_text("SECRET=x\n")
    findings = secret_scan.scan([".env"])
    assert any("FORBIDDEN PATH" in f for f in findings)


def test_candidate_data_path_flagged_as_forbidden(tmp_path, monkeypatch):
    monkeypatch.setattr(secret_scan, "REPO_ROOT", tmp_path)
    (tmp_path / "candidate_data").mkdir()
    (tmp_path / "candidate_data" / "profile.json").write_text("{}")
    findings = secret_scan.scan(["candidate_data/profile.json"])
    assert any("FORBIDDEN PATH" in f for f in findings)
