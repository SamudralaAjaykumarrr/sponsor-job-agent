"""Health/readiness checks (CLAUDE.md Phase 6 section 31).

Health = process alive (no DB access at all -- must return instantly even
if the database is completely down).
Readiness = database reachable + schema compatible + (in Postgres mode)
the shared backend is actually the one configured -- i.e. can this process
actually do useful work right now. Never leaks DB credentials (readiness
details expose backend kind and schema version only, never the DSN)."""

from dataclasses import dataclass

from app import migrations
from app.db import backend as db_backend
from app.db import db_session


@dataclass
class ReadinessResult:
    ready: bool
    database_backend: str
    database_reachable: bool
    schema_version: int
    schema_compatible: bool
    detail: str = ""


def check_readiness() -> ReadinessResult:
    backend = db_backend()
    try:
        with db_session() as conn:
            version = migrations.current_db_version(conn)
            compatible = migrations.is_compatible(conn)
        return ReadinessResult(
            ready=compatible, database_backend=backend, database_reachable=True,
            schema_version=version, schema_compatible=compatible,
            detail="ready" if compatible else "database schema is behind this process's expected version",
        )
    except Exception as exc:  # noqa: BLE001 - readiness must never raise; report unreachable instead
        # Never include the raw exception's message verbatim if it might
        # embed a DSN (some driver errors do) -- report only the exception
        # type name, which is safe and still useful for diagnosis.
        return ReadinessResult(
            ready=False, database_backend=backend, database_reachable=False,
            schema_version=0, schema_compatible=False,
            detail=f"database unreachable ({type(exc).__name__})",
        )
