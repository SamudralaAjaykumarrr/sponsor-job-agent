"""Form fingerprinting + schema-drift detection (CLAUDE.md Phase 8 sections
16-17). Distinct from app.workers.schema_check (that's discovery-payload
drift; this is application-FORM-structure drift for one specific posting)."""

import hashlib
import json

from app.applications.models import FormSnapshot
from app.db import db_session


def compute_fingerprint(snapshot: FormSnapshot) -> str:
    signature = snapshot.field_signature()
    normalized = json.dumps(signature, sort_keys=True)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:40]


def utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def check_and_record_baseline(snapshot: FormSnapshot) -> bool:
    """Returns True if the form structure matches the last known baseline for
    this exact (provider, tenant, external_job_id) -- or if this is the
    first time we've seen it (nothing to drift from yet). Returns False if a
    baseline exists and this fingerprint differs (FORM_SCHEMA_CHANGED).
    Always records/updates the baseline on a match or first-sight; leaves an
    existing baseline untouched on a mismatch so the operator can compare."""
    now = utcnow()
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM application_form_baselines WHERE provider = ? AND tenant_identifier = ? "
            "AND external_job_id = ?",
            (snapshot.provider, snapshot.tenant_identifier, snapshot.external_job_id),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO application_form_baselines "
                "(provider, tenant_identifier, external_job_id, fingerprint, field_signature, "
                " first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (snapshot.provider, snapshot.tenant_identifier, snapshot.external_job_id,
                 snapshot.fingerprint, json.dumps(snapshot.field_signature()), now, now),
            )
            return True
        if row["fingerprint"] == snapshot.fingerprint:
            conn.execute(
                "UPDATE application_form_baselines SET last_seen_at = ? WHERE id = ?", (now, row["id"]),
            )
            return True
        return False
