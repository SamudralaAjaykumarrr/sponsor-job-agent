"""Local multi-process supervisor for application-executor workers (CLAUDE.md
Phase 9 section 39) -- mirrors app.workers.supervisor.Supervisor exactly
(same safety properties: no shell, bounded worker count, SIGINT/SIGTERM
forwarding, prefixed log streaming), just spawning
`python -m app.applications.worker run` instead of the discovery worker CLI.
Unlike the discovery supervisor, no shard index/count is passed -- the
application queue is a single shared claim pool (small volume, rate-limited
by design; CLAUDE.md Phase 9 section 36 "no spray-and-pray"), so sharding it
would add complexity with no benefit."""

import selectors
import signal
import subprocess
import sys
import threading
import time
from typing import Optional

from app import config


class ApplicationSupervisor:
    def __init__(self, worker_count: int, *, python_executable: Optional[str] = None) -> None:
        if worker_count < 1 or worker_count > config.APPLICATION_SUPERVISOR_MAX_WORKERS:
            raise ValueError(
                f"worker_count must be between 1 and {config.APPLICATION_SUPERVISOR_MAX_WORKERS}, got {worker_count}"
            )
        self.worker_count = worker_count
        self.python_executable = python_executable or sys.executable
        self._procs: list[subprocess.Popen] = []
        self._stop = threading.Event()

    def _spawn(self, index: int) -> subprocess.Popen:
        argv = [self.python_executable, "-m", "app.applications.worker", "run"]
        return subprocess.Popen(  # noqa: S603 -- fixed argv list, no shell, no untrusted input
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )

    def start(self) -> None:
        for i in range(self.worker_count):
            self._procs.append(self._spawn(i))

    def _install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame) -> None:
        self._stop.set()

    def stop(self, *, grace_seconds: Optional[int] = None) -> None:
        grace_seconds = grace_seconds if grace_seconds is not None else config.APPLICATION_WORKER_SHUTDOWN_GRACE_SECONDS
        for p in self._procs:
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + grace_seconds
        for p in self._procs:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                p.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=5)

    def _stream_output(self) -> None:
        sel = selectors.DefaultSelector()
        for i, p in enumerate(self._procs):
            if p.stdout:
                sel.register(p.stdout, selectors.EVENT_READ, i)
        while sel.get_map():
            for key, _ in sel.select(timeout=0.5):
                line = key.fileobj.readline()
                if not line:
                    sel.unregister(key.fileobj)
                    continue
                print(f"[app-worker-{key.data}] {line.rstrip()}")
            if self._stop.is_set():
                break

    def run_until_interrupted(self) -> None:
        self._install_signal_handlers()
        self.start()
        try:
            self._stream_output()
        finally:
            self.stop()

    def all_exit_codes(self) -> list[Optional[int]]:
        return [p.poll() for p in self._procs]
