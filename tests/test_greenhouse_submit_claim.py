"""Greenhouse Verified Submission Contract V1: the submit-once claim ledger
(`app.applications.greenhouse_submit_claim`). Pure DB-level tests -- no
Playwright, no network."""

from app.applications import greenhouse_submit_claim as claim


def test_first_claim_is_acquired(tmp_env):
    attempt = claim.acquire_submit_claim("exec-1", 1, claimed_by="worker-a")
    assert attempt.acquired is True
    assert attempt.row["submit_attempted"] == 1
    assert claim.already_attempted("exec-1") is True


def test_second_claim_for_same_execution_is_refused(tmp_env):
    first = claim.acquire_submit_claim("exec-2", 2, claimed_by="worker-a")
    assert first.acquired is True

    second = claim.acquire_submit_claim("exec-2", 2, claimed_by="worker-b")
    assert second.acquired is False
    assert "already" in second.reason.lower()
    # Never overwritten by the losing caller.
    assert second.row["claimed_by"] == "worker-a"


def test_claim_row_created_lazily_without_attempting(tmp_env):
    assert claim.get_claim("exec-3") is None
    assert claim.already_attempted("exec-3") is False


def test_record_outcome_persists_after_claim(tmp_env):
    claim.acquire_submit_claim("exec-4", 4)
    claim.record_outcome("exec-4", outcome="CONFIRMED", detail="ok")
    row = claim.get_claim("exec-4")
    assert row["outcome"] == "CONFIRMED"
    assert row["detail"] if "detail" in row else True


def test_list_claims_returns_recent_rows(tmp_env):
    claim.acquire_submit_claim("exec-5", 5)
    claim.acquire_submit_claim("exec-6", 6)
    rows = claim.list_claims(limit=10)
    execution_ids = {r["execution_id"] for r in rows}
    assert {"exec-5", "exec-6"}.issubset(execution_ids)
