"""Stable runtime worker identity. Deliberately contains NO candidate PII --
only hostname, process id, and a random suffix, matching CLAUDE.md Phase 5
section 4 ("Do not use candidate PII")."""

import os
import socket
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkerIdentity:
    worker_id: str
    hostname: str
    pid: int


def generate_worker_identity() -> WorkerIdentity:
    hostname = socket.gethostname()
    pid = os.getpid()
    worker_id = f"{hostname}-{pid}-{uuid.uuid4().hex[:8]}"
    return WorkerIdentity(worker_id=worker_id, hostname=hostname, pid=pid)
