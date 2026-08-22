"""Dataset versioning (CLAUDE.md Phase 7 section 6). Tracks provenance for
every bulk government-data import so evidence rows can always be traced back
to "which exact dataset/version/fiscal-year produced this row", and so
re-importing the same dataset is detectable/idempotent."""

from datetime import datetime, timezone
from typing import Optional

from app.db import db_session
from app.sponsorship.schema import DatasetStatus


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_or_create_dataset(
    dataset_name: str, dataset_version: str = "", fiscal_year: Optional[int] = None,
    source_url: str = "", schema_version: str = "1",
) -> dict:
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM sponsorship_datasets WHERE dataset_name = ? AND dataset_version = ? AND "
            "fiscal_year IS NOT DISTINCT FROM ?",
            (dataset_name, dataset_version, fiscal_year),
        ).fetchone()
        if row:
            return dict(row)
        now = utcnow()
        cur = conn.execute(
            """INSERT INTO sponsorship_datasets
               (dataset_name, dataset_version, fiscal_year, source_url, schema_version,
                status, record_count, resume_cursor, errors, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, 0, '[]', ?, ?)""",
            (dataset_name, dataset_version, fiscal_year, source_url, schema_version,
             DatasetStatus.PENDING.value, now, now),
        )
        row = conn.execute("SELECT * FROM sponsorship_datasets WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)


def get_dataset(dataset_id: int) -> Optional[dict]:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM sponsorship_datasets WHERE id = ?", (dataset_id,)).fetchone()
        return dict(row) if row else None


def update_dataset(dataset_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = utcnow()
    cleaned = {k: (v.value if hasattr(v, "value") else v) for k, v in fields.items()}
    set_clause = ", ".join(f"{k} = ?" for k in cleaned)
    with db_session() as conn:
        conn.execute(f"UPDATE sponsorship_datasets SET {set_clause} WHERE id = ?", [*cleaned.values(), dataset_id])


def list_datasets(limit: int = 100) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM sponsorship_datasets ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def mark_downloaded(dataset_id: int, checksum: str) -> None:
    update_dataset(dataset_id, downloaded_at=utcnow(), checksum=checksum)


def mark_completed(dataset_id: int, record_count: int) -> None:
    update_dataset(
        dataset_id, status=DatasetStatus.COMPLETED.value, imported_at=utcnow(), record_count=record_count,
    )


def mark_failed(dataset_id: int, error: str) -> None:
    import json

    existing = get_dataset(dataset_id) or {}
    try:
        errors = json.loads(existing.get("errors") or "[]")
    except (ValueError, TypeError):
        errors = []
    errors.append(error)
    update_dataset(dataset_id, status=DatasetStatus.FAILED.value, errors=json.dumps(errors[-50:]))
