"""CLAUDE.md Phase 13 sections 37-39, 62: append-only session checkpoint log
and advisory ordering-anomaly detection. Never touches network/browser."""

from app.applications.checkpoints import (
    CheckpointStage,
    find_ordering_anomalies,
    latest_checkpoint,
    list_checkpoints,
    record_checkpoint,
)


def test_record_and_list_checkpoints_in_order(tmp_env):
    record_checkpoint("s1", CheckpointStage.ENTRY_REACHED, job_id=1)
    record_checkpoint("s1", CheckpointStage.FORM_DISCOVERED, job_id=1)
    rows = list_checkpoints("s1")
    assert [r["checkpoint"] for r in rows] == ["ENTRY_REACHED", "FORM_DISCOVERED"]


def test_latest_checkpoint_returns_most_recent(tmp_env):
    record_checkpoint("s2", CheckpointStage.ENTRY_REACHED)
    record_checkpoint("s2", CheckpointStage.FIELDS_PREPARED)
    latest = latest_checkpoint("s2")
    assert latest["checkpoint"] == "FIELDS_PREPARED"


def test_latest_checkpoint_none_when_no_history(tmp_env):
    assert latest_checkpoint("nonexistent") is None


def test_recording_never_raises_on_bad_input(tmp_env, monkeypatch):
    """CLAUDE.md checkpoints module contract: best-effort, never raises into
    a real discovery/fill pass."""
    from app.applications import checkpoints

    def _boom(*a, **k):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(checkpoints, "db_session", _boom)
    # Must not raise.
    checkpoints.record_checkpoint("s3", CheckpointStage.ENTRY_REACHED)


def test_ordering_anomaly_flagged_on_regression(tmp_env):
    record_checkpoint("s4", CheckpointStage.FIELDS_PREPARED)
    record_checkpoint("s4", CheckpointStage.READY_FOR_FINAL_SUBMIT)
    record_checkpoint("s4", CheckpointStage.ENTRY_REACHED)  # regressed with no reconstruction
    anomalies = find_ordering_anomalies("s4")
    assert len(anomalies) == 1
    assert anomalies[0].to_checkpoint == "ENTRY_REACHED"


def test_no_anomaly_on_forward_progression(tmp_env):
    record_checkpoint("s5", CheckpointStage.ENTRY_REACHED)
    record_checkpoint("s5", CheckpointStage.FORM_DISCOVERED)
    record_checkpoint("s5", CheckpointStage.FIELDS_PREPARED)
    record_checkpoint("s5", CheckpointStage.READY_FOR_FINAL_SUBMIT)
    assert find_ordering_anomalies("s5") == []


def test_user_action_required_is_never_flagged_as_regression(tmp_env):
    """USER_ACTION_REQUIRED can legitimately occur at almost any point --
    never treated as an anomalous regression."""
    record_checkpoint("s6", CheckpointStage.READY_FOR_FINAL_SUBMIT)
    record_checkpoint("s6", CheckpointStage.USER_ACTION_REQUIRED)
    assert find_ordering_anomalies("s6") == []
