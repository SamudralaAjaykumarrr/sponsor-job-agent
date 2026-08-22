"""Stable runtime worker identity. Deliberately contains NO candidate PII --
only hostname, process id, and a random suffix, matching CLAUDE.md Phase 5
section 4 ("Do not use candidate PII").

Phase 6 (CLAUDE.md section 8): a worker may now run on a separate machine
from every other worker, coordinating only through a shared database, so
identity gains operational metadata (software/schema/capability version,
backend) that lets a mixed-version fleet be detected on the dashboard
rather than silently misbehaving -- see docs/distributed-workers.md."""

import os
import socket
import uuid
from dataclasses import dataclass

from app import db
from app import migrations
from app.version import WORKER_SOFTWARE_VERSION, capability_fingerprint


@dataclass(frozen=True)
class WorkerIdentity:
    worker_id: str
    hostname: str
    pid: int
    worker_version: str = WORKER_SOFTWARE_VERSION
    schema_version: int = 0
    capability_version: str = ""
    backend: str = "sqlite"


def generate_worker_identity() -> WorkerIdentity:
    hostname = socket.gethostname()
    pid = os.getpid()
    worker_id = f"{hostname}-{pid}-{uuid.uuid4().hex[:8]}"
    return WorkerIdentity(
        worker_id=worker_id,
        hostname=hostname,
        pid=pid,
        worker_version=WORKER_SOFTWARE_VERSION,
        schema_version=migrations.CURRENT_SCHEMA_VERSION,
        capability_version=capability_fingerprint(),
        backend=db.backend(),
    )
