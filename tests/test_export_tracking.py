"""CLAUDE.md Phase 15 section 75: safe job/application tracking export --
never includes the candidate's private profile."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import export_tracking  # noqa: E402
from app.jobs_repo import insert_job
from app.models import Job


def test_export_rows_never_include_candidate_profile_fields(tmp_env):
    insert_job(Job(title="Backend Engineer", company="Acme", description="x"))
    rows = export_tracking.export_rows()
    assert len(rows) == 1
    for forbidden in ("email", "phone", "ssn", "full_name", "resume_content", "cover_letter_text"):
        assert forbidden not in rows[0]


def test_export_rows_cover_expected_tracking_fields(tmp_env):
    insert_job(Job(title="Backend Engineer", company="Acme", description="x"))
    rows = export_tracking.export_rows()
    for field in ("company", "title", "application_state", "sponsorship_status", "priority_tier"):
        assert field in rows[0]


def test_write_csv_and_json(tmp_env, tmp_path):
    insert_job(Job(title="Backend Engineer", company="Acme", description="x"))
    rows = export_tracking.export_rows()

    csv_path = tmp_path / "out.csv"
    export_tracking.write_csv(rows, csv_path)
    assert csv_path.exists() and csv_path.read_text().splitlines()[0].startswith("id,")

    json_path = tmp_path / "out.json"
    export_tracking.write_json(rows, json_path)
    parsed = json.loads(json_path.read_text())
    assert parsed[0]["company"] == "Acme"
