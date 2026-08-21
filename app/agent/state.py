import threading
from datetime import datetime, timezone

from app import config

_lock = threading.Lock()
_state = {
    "enabled": config.AGENT_ENABLED,
    "running": False,
    "last_cycle_started_at": None,
    "last_cycle_finished_at": None,
    "next_cycle_at": None,
    "last_cycle_summary": None,
}


def is_enabled() -> bool:
    with _lock:
        return _state["enabled"]


def set_enabled(value: bool) -> None:
    with _lock:
        _state["enabled"] = value


def mark_cycle_start() -> None:
    with _lock:
        _state["running"] = True
        _state["last_cycle_started_at"] = datetime.now(timezone.utc).isoformat()


def mark_cycle_end(summary: dict) -> None:
    with _lock:
        _state["running"] = False
        _state["last_cycle_finished_at"] = datetime.now(timezone.utc).isoformat()
        _state["last_cycle_summary"] = summary


def set_next_cycle_at(ts: str | None) -> None:
    with _lock:
        _state["next_cycle_at"] = ts


def get_status() -> dict:
    with _lock:
        return dict(_state)
