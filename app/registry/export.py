"""Streaming/batched registry export -- JSONL by default (recommended for
scale). Includes provenance. Never includes candidate/application/resume
data, since it only ever reads the registry_* tables. See CLAUDE.md Phase 4
section 28."""

import json
from pathlib import Path
from typing import Iterator

from app.db import db_session

_BATCH_SIZE = 1000


def _iter_portals_with_company() -> Iterator[dict]:
    """Keyset-paginated streaming read -- never loads the whole registry into
    memory regardless of table size."""
    after_id = 0
    with db_session() as conn:
        while True:
            rows = conn.execute(
                """SELECT rp.*, rc.normalized_name, rc.display_name, rc.primary_domain,
                          rc.careers_home_url AS company_careers_home_url, rc.country AS company_country
                   FROM registry_portals rp
                   JOIN registry_companies rc ON rc.id = rp.company_id
                   WHERE rp.id > ?
                   ORDER BY rp.id ASC LIMIT ?""",
                (after_id, _BATCH_SIZE),
            ).fetchall()
            if not rows:
                return
            for row in rows:
                yield dict(row)
            after_id = rows[-1]["id"]


def _provenance_for(conn, portal_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT source_type, source_name, source_url, observed_at, evidence, confidence FROM registry_provenance WHERE portal_id = ?",
        (portal_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def export_jsonl(path: str | Path) -> int:
    """Writes one JSON object per line, streamed in bounded batches. Returns
    the number of portal rows written."""
    count = 0
    with open(path, "w", encoding="utf-8") as f, db_session() as conn:
        for portal in _iter_portals_with_company():
            portal["provenance"] = _provenance_for(conn, portal["id"])
            f.write(json.dumps(portal, default=str) + "\n")
            count += 1
    return count


def export_json(path: str | Path) -> int:
    records = []
    with db_session() as conn:
        for portal in _iter_portals_with_company():
            portal["provenance"] = _provenance_for(conn, portal["id"])
            records.append(portal)
    Path(path).write_text(json.dumps(records, indent=2, default=str), encoding="utf-8")
    return len(records)
