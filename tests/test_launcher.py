"""Tsenta Remaining-Gaps Closure V2, section 9 (LAUNCHER tests). Exercises
only the pure, injectable logic in app.launcher -- no real subprocess, no
real network, no real browser (those are structurally untestable from an
automated Linux test run; see app/launcher.py's own docstring)."""

from app.launcher import (
    LaunchOutcome,
    check_readiness,
    detect_existing_instance,
    format_user_friendly_error,
    is_wsl,
    wait_for_readiness,
)


class _Resp:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


def _fake_get_healthy_and_ready(url, timeout):
    if "/health" in url:
        return _Resp(200)
    return _Resp(200, {"ready": True})


def _fake_get_healthy_not_ready(url, timeout):
    if "/health" in url:
        return _Resp(200)
    return _Resp(503, {"ready": False, "detail": "database not yet ready"})


def _fake_get_connection_refused(url, timeout):
    raise ConnectionError("refused")


def _fake_get_wrong_service(url, timeout):
    return _Resp(404)


def test_check_readiness_when_fully_ready():
    result = check_readiness("http://x", http_get=_fake_get_healthy_and_ready)
    assert result.healthy is True
    assert result.ready is True


def test_check_readiness_when_healthy_but_db_not_ready():
    result = check_readiness("http://x", http_get=_fake_get_healthy_not_ready)
    assert result.healthy is True
    assert result.ready is False
    assert "database" in result.detail


def test_check_readiness_when_nothing_listening():
    result = check_readiness("http://x", http_get=_fake_get_connection_refused)
    assert result.healthy is False
    assert result.ready is False


def test_check_readiness_when_something_else_answers():
    result = check_readiness("http://x", http_get=_fake_get_wrong_service)
    assert result.healthy is False
    assert result.ready is False
    assert "404" in result.detail


def test_detect_existing_instance_already_running():
    result = detect_existing_instance("http://x", http_get=_fake_get_healthy_and_ready)
    assert result.outcome == LaunchOutcome.ALREADY_RUNNING
    assert result.ok is True


def test_detect_existing_instance_nothing_listening_is_not_a_conflict():
    result = detect_existing_instance("http://x", http_get=_fake_get_connection_refused)
    assert result.outcome != LaunchOutcome.PORT_CONFLICT
    assert result.outcome != LaunchOutcome.ALREADY_RUNNING


def test_detect_existing_instance_port_conflict_is_distinct():
    result = detect_existing_instance("http://x", http_get=_fake_get_wrong_service)
    assert result.outcome == LaunchOutcome.PORT_CONFLICT


def test_detect_existing_instance_mid_startup_counts_as_already_running():
    """A second launch while the first is still becoming ready must not
    spawn a duplicate server -- it should be treated as ALREADY_RUNNING so
    the launcher waits instead of starting a second process."""
    result = detect_existing_instance("http://x", http_get=_fake_get_healthy_not_ready)
    assert result.outcome == LaunchOutcome.ALREADY_RUNNING


def test_wait_for_readiness_bounded_timeout_never_hangs():
    fake_time = {"t": 0.0}

    def fake_sleep(seconds):
        fake_time["t"] += seconds

    def fake_now():
        return fake_time["t"]

    result = wait_for_readiness(
        "http://x", timeout_seconds=5, interval_seconds=1,
        http_get=_fake_get_connection_refused, sleep=fake_sleep, now=fake_now,
    )
    assert result.outcome == LaunchOutcome.TIMED_OUT
    assert result.elapsed_seconds >= 5


def test_wait_for_readiness_succeeds_once_ready_appears():
    calls = {"n": 0}

    def fake_get(url, timeout):
        if "/health" in url:
            return _Resp(200)
        calls["n"] += 1
        if calls["n"] < 3:
            return _Resp(503, {"ready": False})
        return _Resp(200, {"ready": True})

    fake_time = {"t": 0.0}

    def fake_sleep(seconds):
        fake_time["t"] += seconds

    def fake_now():
        return fake_time["t"]

    result = wait_for_readiness(
        "http://x", timeout_seconds=30, interval_seconds=0.5,
        http_get=fake_get, sleep=fake_sleep, now=fake_now,
    )
    assert result.outcome == LaunchOutcome.STARTED
    assert result.ok is True


def test_wait_for_readiness_healthy_but_never_db_ready_is_not_ready_not_timed_out():
    result = wait_for_readiness(
        "http://x", timeout_seconds=2, interval_seconds=1,
        http_get=_fake_get_healthy_not_ready, sleep=lambda s: None, now=iter([0, 1, 2, 3]).__next__,
    )
    assert result.outcome == LaunchOutcome.NOT_READY


def test_format_user_friendly_error_never_contains_traceback_shape():
    msg = format_user_friendly_error(ConnectionError("refused"))
    assert "Traceback" not in msg
    assert "File \"" not in msg
    assert len(msg) < 200


def test_format_user_friendly_error_unknown_exception_still_readable():
    class SomeWeirdInternalError(Exception):
        pass

    msg = format_user_friendly_error(SomeWeirdInternalError("obscure internal detail"))
    assert "Traceback" not in msg
    assert msg  # never empty


def test_is_wsl_is_a_bool_and_never_raises():
    assert isinstance(is_wsl(), bool)
