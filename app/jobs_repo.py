import sqlite3
from typing import Optional

from app.db import db_session
from app.models import Job, utcnow

_COLUMNS = [
    "title", "company", "location", "description", "url", "source",
    "published_at", "first_seen_at",
    "work_arrangement", "sponsorship_status", "sponsorship_evidence",
    "freshness_tier",
    "technical_match_score", "matched_skills", "gap_skills",
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
    if filters.get("high_priority"):
        clauses.append("priority_tier IN ('P1_REMOTE_CONFIRMED', 'P2_REMOTE_LIKELY', 'P3_HYBRID_CONFIRMED')")

    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY priority_score DESC, first_seen_at DESC"

    with db_session() as conn:
        rows = conn.execute(query, params).fetchall()
        return [_row_to_job(r) for r in rows]
