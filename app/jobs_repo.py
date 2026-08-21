import json
import sqlite3
from typing import Optional

from app.db import db_session
from app.models import Job, utcnow

_COLUMNS = [
    "title", "company", "location", "description", "url", "source",
    "provider", "external_job_id", "employment_type", "salary_min", "salary_max",
    "dedup_fingerprint",
    "published_at", "first_seen_at", "last_seen_at",
    "work_arrangement", "sponsorship_status", "sponsorship_evidence",
    "freshness_tier", "freshness_minutes",
    "technical_match_score", "matched_skills", "gap_skills", "score_breakdown",
    "priority_tier", "priority_score",
    "application_state", "mode",
    "resume_docx_path", "resume_pdf_path", "resume_txt_path",
    "job_analysis_path", "application_answers_path", "cover_letter_path",
    "notes", "created_at", "updated_at",
]


def _row_to_job(row: sqlite3.Row) -> Job:
    data = dict(row)
    return Job.model_validate(data)


def insert_job(job: Job) -> int:
    with db_session() as conn:
        values = [getattr(job, col) for col in _COLUMNS]
        values = [v.value if hasattr(v, "value") else v for v in values]
        placeholders = ", ".join("?" for _ in _COLUMNS)
        cols = ", ".join(_COLUMNS)
        cur = conn.execute(f"INSERT INTO jobs ({cols}) VALUES ({placeholders})", values)
        return cur.lastrowid


def update_job(job_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = utcnow()
    cleaned = {}
    for k, v in fields.items():
        cleaned[k] = v.value if hasattr(v, "value") else v
    set_clause = ", ".join(f"{k} = ?" for k in cleaned)
    with db_session() as conn:
        conn.execute(f"UPDATE jobs SET {set_clause} WHERE id = ?", [*cleaned.values(), job_id])


def get_job(job_id: int) -> Optional[Job]:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None


def get_job_by_provider_external_id(provider: str, external_job_id: str) -> Optional[Job]:
    if not external_job_id:
        return None
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE provider = ? AND external_job_id = ?",
            (provider, external_job_id),
        ).fetchone()
        return _row_to_job(row) if row else None


def get_job_by_fingerprint(fingerprint: str) -> Optional[Job]:
    if not fingerprint:
        return None
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE dedup_fingerprint = ? ORDER BY id ASC LIMIT 1",
            (fingerprint,),
        ).fetchone()
        return _row_to_job(row) if row else None


def touch_last_seen(job_id: int, last_seen_at: str) -> None:
    with db_session() as conn:
        conn.execute("UPDATE jobs SET last_seen_at = ? WHERE id = ?", (last_seen_at, job_id))


def list_jobs(filters: Optional[dict] = None) -> list[Job]:
    filters = filters or {}
    query = "SELECT * FROM jobs"
    clauses = []
    params = []

    if filters.get("work_arrangement"):
        clauses.append("work_arrangement = ?")
        params.append(filters["work_arrangement"])
    if filters.get("sponsorship_status"):
        clauses.append("sponsorship_status = ?")
        params.append(filters["sponsorship_status"])
    if filters.get("application_state"):
        clauses.append("application_state = ?")
        params.append(filters["application_state"])
    if filters.get("fresh_under_1hr"):
        clauses.append("freshness_tier = ?")
        params.append("MAXIMUM")
    if filters.get("fresh_under_6hr"):
        clauses.append("freshness_minutes IS NOT NULL AND freshness_minutes <= 360")
    if filters.get("high_priority"):
        clauses.append("priority_tier IN ('P1_REMOTE_CONFIRMED', 'P2_REMOTE_LIKELY', 'P3_HYBRID_CONFIRMED')")

    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY priority_score DESC, first_seen_at DESC"

    with db_session() as conn:
        rows = conn.execute(query, params).fetchall()
        return [_row_to_job(r) for r in rows]


def insert_discovery_cycle(summary: dict) -> int:
    with db_session() as conn:
        cur = conn.execute(
            """INSERT INTO discovery_cycles
               (started_at, finished_at, providers, jobs_fetched, jobs_new, jobs_deduplicated,
                jobs_analyzed, confirmed_sponsors, likely_sponsors, hard_skips,
                packages_generated, errors, duration_seconds)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                summary.get("started_at"),
                summary.get("finished_at"),
                ",".join(summary.get("providers", [])),
                summary.get("jobs_fetched", 0),
                summary.get("jobs_new", 0),
                summary.get("jobs_deduplicated", 0),
                summary.get("jobs_analyzed", 0),
                summary.get("confirmed_sponsors", 0),
                summary.get("likely_sponsors", 0),
                summary.get("hard_skips", 0),
                summary.get("packages_generated", 0),
                json.dumps(summary.get("errors", [])),
                summary.get("duration_seconds", 0.0),
            ),
        )
        return cur.lastrowid


def list_discovery_cycles(limit: int = 20) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM discovery_cycles ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["errors"] = json.loads(d.get("errors") or "[]")
            result.append(d)
        return result


def record_state_change(job_id: int, from_state: Optional[str], to_state: str, actor: str = "system") -> None:
    with db_session() as conn:
        conn.execute(
            "INSERT INTO application_state_history (job_id, from_state, to_state, changed_at, actor) "
            "VALUES (?, ?, ?, ?, ?)",
            (job_id, from_state, to_state, utcnow(), actor),
        )


def get_state_history(job_id: int) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM application_state_history WHERE job_id = ? ORDER BY id ASC",
            (job_id,),
        ).fetchall()
        return [dict(r) for r in rows]
