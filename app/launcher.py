"""No-terminal product startup support (Tsenta Remaining-Gaps Closure V2,
section 2).

This module holds the PURE, deterministically-testable logic behind the
one-action local launcher (`launch.sh`): detecting whether the product is
already running (so a second launch is a safe no-op, never a duplicate
server), waiting for genuine readiness (never a fixed sleep), and turning a
raw exception into a short, human-readable message -- no stack trace shown
to a normal user.

What is deliberately NOT here, because it cannot be meaningfully unit-tested
in this environment: actually spawning the uvicorn process, and actually
opening a browser window on the Windows side from WSL. Those live in
`launch.sh` as a thin shell wrapper around this module's `python -m
app.launcher wait` / `python -m app.launcher check` entry points. No
credentials are read or embedded anywhere in this module; no path is
hardcoded to a specific user's home directory (the caller always supplies
`host`/`port`, defaulting to the same 127.0.0.1:8000 `start.sh` already
uses)."""

import time
from dataclasses import dataclass
from typing import Optional


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


class LaunchOutcome:
    ALREADY_RUNNING = "ALREADY_RUNNING"          # healthy instance already up -- safe to just open the browser
    STARTED = "STARTED"                          # we waited and it became ready
    TIMED_OUT = "TIMED_OUT"                       # never became ready within the bound
    PORT_CONFLICT = "PORT_CONFLICT"               # something else is listening on this port, not us
    NOT_READY = "NOT_READY"                       # process up (/health ok) but DB/schema not ready yet


@dataclass(frozen=True)
class ReadinessCheck:
    healthy: bool
    ready: bool
    detail: str = ""


@dataclass(frozen=True)
class LaunchResult:
    outcome: str
    detail: str
    elapsed_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.outcome in (LaunchOutcome.ALREADY_RUNNING, LaunchOutcome.STARTED)


def format_user_friendly_error(exc: BaseException) -> str:
    """Turns any exception into ONE short, plain-language line -- never a
    stack trace, never an internal module path or enum name. The caller is
    responsible for logging the full exception elsewhere (e.g. to a log
    file) for a developer to diagnose; this function's output is what a
    normal user sees on their screen."""
    name = type(exc).__name__
    text = str(exc).strip()
    known = {
        "ConnectionError": "Could not reach the local server.",
        "ConnectError": "Could not reach the local server.",
        "TimeoutException": "The local server took too long to respond.",
        "Timeout": "The local server took too long to respond.",
        "ConnectTimeout": "The local server took too long to respond.",
        "OSError": "A system error prevented the server from starting.",
        "PermissionError": "Permission was denied while starting the server.",
    }
    prefix = known.get(name, "Something went wrong while starting Sponsor Job Agent.")
    if text and len(text) < 160:
        return f"{prefix} ({text})"
    return prefix


def check_readiness(base_url: str, *, http_get=None, timeout: float = 3.0) -> ReadinessCheck:
    """One probe of /health then /readiness. `http_get` is an injectable
    `Callable[[str, float], SimpleNamespace-like]` returning an object with
    `.status_code` and `.json()` -- defaults to a real `httpx.get` at call
    time so this module has no hard import-time dependency and tests never
    need a real server or real network."""
    if http_get is None:
        import httpx

        def http_get(url: str, t: float):
            return httpx.get(url, timeout=t)

    try:
        health_resp = http_get(f"{base_url}/health", timeout)
    except Exception as exc:
        return ReadinessCheck(healthy=False, ready=False, detail=format_user_friendly_error(exc))

    if health_resp.status_code != 200:
        return ReadinessCheck(
            healthy=False, ready=False,
            detail=f"the server responded but /health returned status {health_resp.status_code}",
        )

    try:
        readiness_resp = http_get(f"{base_url}/readiness", timeout)
    except Exception as exc:
        # The process is alive (/health passed) but /readiness itself
        # couldn't be reached -- report healthy-but-not-ready rather than
        # collapsing this into the same bucket as "nothing is listening".
        return ReadinessCheck(healthy=True, ready=False, detail=format_user_friendly_error(exc))

    if readiness_resp.status_code != 200:
        detail = "database not yet ready"
        try:
            body = readiness_resp.json()
            if isinstance(body, dict) and body.get("detail"):
                detail = str(body["detail"])
        except Exception:
            pass
        return ReadinessCheck(healthy=True, ready=False, detail=detail)

    return ReadinessCheck(healthy=True, ready=True, detail="ready")


def wait_for_readiness(
    base_url: str, *, timeout_seconds: float = 60.0, interval_seconds: float = 0.5,
    http_get=None, sleep=None, now=None,
) -> LaunchResult:
    """Polls `check_readiness` at a fixed interval until either it reports
    ready, or `timeout_seconds` elapses -- never a single fixed sleep, and
    never an unbounded wait. `sleep`/`now` are injectable so tests run
    instantly and deterministically instead of actually waiting in real
    time."""
    if sleep is None:
        sleep = time.sleep
    if now is None:
        now = time.monotonic

    start = now()
    last_detail = "server has not responded yet"
    while True:
        check = check_readiness(base_url, http_get=http_get)
        elapsed = now() - start
        if check.ready:
            return LaunchResult(LaunchOutcome.STARTED, "server is ready", elapsed_seconds=elapsed)
        last_detail = check.detail
        if elapsed >= timeout_seconds:
            outcome = LaunchOutcome.NOT_READY if check.healthy else LaunchOutcome.TIMED_OUT
            return LaunchResult(
                outcome,
                f"Sponsor Job Agent did not become ready within {int(timeout_seconds)}s: {last_detail}",
                elapsed_seconds=elapsed,
            )
        sleep(interval_seconds)


def detect_existing_instance(
    base_url: str, *, http_get=None,
) -> LaunchResult:
    """Called BEFORE spawning a new server process. Returns ALREADY_RUNNING
    only when a genuinely healthy Sponsor Job Agent instance already answers
    -- the launcher should then skip starting a second process and just open
    the browser (idempotent start, no duplicate instance). Any other
    response (connection refused, or a response that isn't ours) is reported
    as "no conflict, safe to start" via a non-ALREADY_RUNNING result with
    `detail` explaining why, EXCEPT when something answers on the port but
    fails our own health contract -- that is a genuine port conflict the
    launcher must surface rather than silently trying to bind anyway."""
    check = check_readiness(base_url, http_get=http_get)
    if check.ready:
        return LaunchResult(LaunchOutcome.ALREADY_RUNNING, "Sponsor Job Agent is already running and ready")
    if check.healthy and not check.ready:
        # Something answering /health as us, just not ready yet (e.g. mid
        # migration) -- not a conflict, but also not a fresh start; the
        # caller should wait rather than spawn a second process.
        return LaunchResult(LaunchOutcome.ALREADY_RUNNING, f"an instance is already starting up: {check.detail}")
    return LaunchResult(LaunchOutcome.PORT_CONFLICT if _looks_like_conflict(check.detail) else LaunchOutcome.TIMED_OUT,
                         check.detail)


def _looks_like_conflict(detail: str) -> bool:
    """A response arrived but didn't pass our own /health contract (status
    != 200, or a non-JSON body) -- as opposed to no response at all
    (connection refused), which just means nothing is listening yet."""
    lowered = (detail or "").lower()
    return "status" in lowered and "connect" not in lowered and "timed out" not in lowered


def base_url(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> str:
    return f"http://{host}:{port}"


def is_wsl() -> bool:
    """Best-effort, read-only detection of Windows Subsystem for Linux --
    never used for anything except deciding how to open a browser window;
    every code path in this module works identically whether or not this
    returns True."""
    try:
        with open("/proc/version", "r", encoding="utf-8", errors="ignore") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def _cli(argv: Optional[list[str]] = None) -> int:
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(prog="python -m app.launcher")
    parser.add_argument("command", choices=["check", "wait", "detect"])
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args(argv)

    url = base_url(args.host, args.port)
    if args.command == "check":
        result = check_readiness(url)
        print(json.dumps({"healthy": result.healthy, "ready": result.ready, "detail": result.detail}))
        return 0 if result.ready else 1
    if args.command == "detect":
        result = detect_existing_instance(url)
        print(json.dumps({"outcome": result.outcome, "detail": result.detail}))
        return 0
    result = wait_for_readiness(url, timeout_seconds=args.timeout)
    print(json.dumps({"outcome": result.outcome, "detail": result.detail, "elapsed_seconds": result.elapsed_seconds}))
    return 0 if result.ok else 1


if __name__ == "__main__":
    import sys

    sys.exit(_cli())
