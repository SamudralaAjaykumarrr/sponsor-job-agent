"""Canary Candidate Pool Expansion + Multi-Provider Readiness V1: discovery
expansion (the new registry seed file) and candidate-ranking regression
tests. No real network -- the seed file is validated via a dry-run import
against a fresh test DB (deterministic, local only); feasibility scenarios
reuse tests/test_canary_feasibility.py's existing established pattern
(jobs inserted directly, no provider network touchpoint)."""

from pathlib import Path

import pytest

from app.applications.canary_feasibility import FeasibilityVerdict, evaluate_canary_feasibility
from app.jobs_repo import insert_job, get_job
from app.models import ApplicationState, Job, SponsorshipStatus
from app.registry.importers import import_file

SEED_PATH = Path(__file__).resolve().parents[1] / "data" / "registry_seed" / "canary_expansion_seed_v1.csv"

SENIOR_DESCRIPTION = """
We are hiring a Senior Software Engineer to lead our platform team.

Requirements:
- 8+ years of professional software engineering experience
- Deep expertise in distributed systems architecture
- Experience mentoring senior and staff engineers
- Full-time, permanent position. H-1B sponsorship is available for this role.
"""

REALISTIC_DESCRIPTION = """
We are hiring a Backend Software Engineer II to build and operate our core services.

Requirements:
- 2-4 years of experience with Python
- Experience with FastAPI or Flask
- Experience building and consuming REST APIs
- Full-time, permanent position. H-1B sponsorship is available for this role.
"""


def _make_job(tmp_env, **overrides) -> Job:
    defaults = dict(
        title="Backend Software Engineer II", company="Acme Corp", company_identifier="acme-corp",
        location="Remote - US", description=REALISTIC_DESCRIPTION, provider="mock_ats",
        canonical_url="https://example.com/jobs/1", url="https://example.com/jobs/1",
        employment_type="full_time", sponsorship_status=SponsorshipStatus.CONFIRMED_SPONSOR,
        technical_match_score=80.0, matched_skills="python,fastapi,postgresql,docker",
        gap_skills="", application_state=ApplicationState.ANALYZED,
    )
    defaults.update(overrides)
    job_id = insert_job(Job(**defaults))
    return get_job(job_id)


# --- discovery expansion: seed file structural validity ---------------------

def test_seed_file_exists_and_is_nonempty():
    assert SEED_PATH.exists(), SEED_PATH
    text = SEED_PATH.read_text()
    lines = [l for l in text.splitlines() if l.strip()]
    assert len(lines) > 1  # header + at least one data row


def test_seed_file_has_no_duplicate_tenant_identifiers():
    import csv
    seen = set()
    with open(SEED_PATH, newline="") as f:
        for row in csv.DictReader(f):
            key = (row["provider"], row["tenant_identifier"])
            assert key not in seen, f"duplicate tenant in seed file: {key}"
            seen.add(key)


def test_seed_file_imports_cleanly_dry_run(tmp_env):
    summary = import_file(SEED_PATH, source_name="canary_expansion_seed_v1_test", dry_run=True)
    assert summary.rows_invalid == 0, summary
    assert summary.rows_total > 0


def test_seed_file_imports_and_creates_candidate_portals(tmp_env):
    summary = import_file(SEED_PATH, source_name="canary_expansion_seed_v1_test")
    assert summary.rows_invalid == 0
    assert summary.rows_created == summary.rows_total
    # Re-importing the identical file must never duplicate rows (idempotent).
    summary2 = import_file(SEED_PATH, source_name="canary_expansion_seed_v1_test")
    assert summary2.rows_created == 0
    assert summary2.rows_updated == summary.rows_total or summary2.rows_skipped == summary.rows_total


# --- candidate selection: seniority mismatch is reviewed/rejected -----------

def test_senior_role_with_high_year_requirement_is_reviewed_not_passed(tmp_env):
    job = _make_job(tmp_env, title="Senior Software Engineer, Platform", description=SENIOR_DESCRIPTION)
    result = evaluate_canary_feasibility(job)
    # A genuine seniority mismatch must never silently PASS the gate --
    # either REVIEW or REJECT, never treated as equally strong as a
    # realistic-level match.
    assert result.verdict != FeasibilityVerdict.PASS, result.as_dict()


def test_realistic_level_backend_role_can_pass(tmp_env):
    job = _make_job(tmp_env)
    result = evaluate_canary_feasibility(job)
    assert result.verdict == FeasibilityVerdict.PASS, result.as_dict()
    assert result.experience.verdict == FeasibilityVerdict.PASS


# --- candidate ranking: no-sponsor/unknown stay excluded regardless of score

def test_no_sponsorship_job_never_outranks_via_feasibility_despite_high_priority_score(tmp_env):
    strong = _make_job(tmp_env, title="Backend Engineer", priority_score=1.0)
    weak_no_sponsor = _make_job(
        tmp_env, title="Backend Engineer", sponsorship_status=SponsorshipStatus.NO_SPONSORSHIP,
        priority_score=99.0,
    )
    strong_result = evaluate_canary_feasibility(strong)
    weak_result = evaluate_canary_feasibility(weak_no_sponsor)
    assert strong_result.verdict == FeasibilityVerdict.PASS
    assert weak_result.verdict == FeasibilityVerdict.REJECT
    assert weak_result.sponsorship.verdict == FeasibilityVerdict.REJECT
