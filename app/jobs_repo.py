import json
import sqlite3
from typing import Optional

from app.db import db_session
from app.models import Job, utcnow

_COLUMNS = [
    "title", "company", "location", "description", "url", "source",
    "provider", "external_job_id", "employment_type", "salary_min", "salary_max",
    "dedup_fingerprint",
    "company_identifier", "city", "state", "country", "remote_status",
    "department", "team", "office", "source_url", "canonical_url",
    "salary_currency", "salary_period", "provider_metadata",
    "published_at", "first_seen_at", "last_seen_at", "freshness_source",
    "work_arrangement", "sponsorship_status", "sponsorship_evidence",
    "sponsorship_decision_version", "jd_sponsorship_fingerprint",
    "sponsorship_conflict", "sponsorship_blocking_reason",
    "freshness_tier", "freshness_minutes",
    "technical_match_score", "matched_skills", "gap_skills", "score_breakdown",
    "priority_tier", "priority_score",
    "application_state", "mode",
    "resume_docx_path", "resume_pdf_path", "resume_txt_path",
    "job_analysis_path", "application_answers_path", "cover_letter_path",
    "notes", "correlation_id", "created_at", "updated_at",
]


def _row_to_job(row: sqlite3.Row) -> Job:
    data = dict(row)
    return Job.model_validate(data)


def _coerce_sql_value(v):
    """CLAUDE.md Phase 6's schema rule: boolean flags stay INTEGER (0/1) in
    BOTH backends -- SQLite silently accepts a Python bool (coerces it), but
    psycopg maps a Python bool to Postgres's native `boolean` type, which
    then conflicts with an `INTEGER` column (a real DatatypeMismatch caught
    live by this phase's own Postgres acceptance testing, e.g.
    jobs.sponsorship_conflict). Every value written to a `jobs` row must be
    coerced explicitly rather than relying on SQLite's permissiveness."""
    if hasattr(v, "value"):
        return v.value
    if isinstance(v, bool):
        return int(v)
    return v


def insert_job(job: Job) -> int:
    with db_session() as conn:
        values = [_coerce_sql_value(getattr(job, col)) for col in _COLUMNS]
        placeholders = ", ".join("?" for _ in _COLUMNS)
        cols = ", ".join(_COLUMNS)
        cur = conn.execute(f"INSERT INTO jobs ({cols}) VALUES ({placeholders})", values)
        return cur.lastrowid


def update_job(job_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = utcnow()
    cleaned = {k: _coerce_sql_value(v) for k, v in fields.items()}
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


def get_job_by_canonical_url(canonical_url: str) -> Optional[Job]:
    """Cross-provider dedup key: the same requisition syndicated from two
    sources (or migrated to a new ATS) should still resolve to one job row
    when its canonical apply/source URL matches."""
    if not canonical_url:
        return None
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE canonical_url = ? ORDER BY id ASC LIMIT 1",
            (canonical_url,),
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
    if filters.get("historical_strength"):
        # CLAUDE.md Phase 7 section 30: an ADDITIONAL filter axis on top of
        # (never a replacement for) sponsorship_status -- matched by company
        # display name against the cached employer profile. Best-effort
        # (display-name match, not full identity resolution) -- never used
        # to change a job's own sponsorship_status.
        clauses.append(
            "company IN (SELECT rc.display_name FROM registry_companies rc "
            "JOIN employer_sponsorship_profile esp ON esp.company_id = rc.id "
            "WHERE esp.historical_strength = ?)"
        )
        params.append(filters["historical_strength"])

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


def start_discovery_cycle(started_at: str, providers: list[str]) -> int:
    """Allocates a discovery_cycles row up front (before providers are
    fetched) so per-tenant provenance/discovery_log rows can reference a
    cycle_id while the cycle is still running. finalize_discovery_cycle fills
    in the final stats once the cycle completes."""
    with db_session() as conn:
        cur = conn.execute(
            """INSERT INTO discovery_cycles (started_at, providers, errors)
               VALUES (?, ?, '[]')""",
            (started_at, ",".join(providers)),
        )
        return cur.lastrowid


def finalize_discovery_cycle(cycle_id: int, summary: dict) -> None:
    with db_session() as conn:
        conn.execute(
            """UPDATE discovery_cycles SET
               finished_at = ?, providers = ?, jobs_fetched = ?, jobs_new = ?,
               jobs_deduplicated = ?, jobs_analyzed = ?, confirmed_sponsors = ?,
               likely_sponsors = ?, hard_skips = ?, packages_generated = ?,
               errors = ?, duration_seconds = ?
               WHERE id = ?""",
            (
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
                cycle_id,
            ),
        )


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


# --- Provenance: retain every source a job was discovered from, even when it
# dedupes into one canonical job row. ---------------------------------------

def record_provenance(
    job_id: int,
    provider: str,
    provider_job_id: str,
    source_url: str = "",
    registry_id: Optional[int] = None,
    discovery_cycle_id: Optional[int] = None,
) -> None:
    now = utcnow()
    with db_session() as conn:
        existing = conn.execute(
            "SELECT id FROM job_provenance WHERE job_id = ? AND provider = ? AND provider_job_id = ?",
            (job_id, provider, provider_job_id),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE job_provenance SET last_seen_at = ?, discovery_cycle_id = ? WHERE id = ?",
                (now, discovery_cycle_id, existing["id"]),
            )
            return
        conn.execute(
            """INSERT INTO job_provenance
               (job_id, provider, registry_id, source_url, provider_job_id,
                discovery_cycle_id, first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (job_id, provider, registry_id, source_url, provider_job_id, discovery_cycle_id, now, now),
        )


def list_provenance(job_id: int) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM job_provenance WHERE job_id = ? ORDER BY id ASC", (job_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# --- Discovery log: per-tenant/provider observability for one cycle. -------

def insert_discovery_log(entry: dict) -> int:
    with db_session() as conn:
        cur = conn.execute(
            """INSERT INTO discovery_log
               (cycle_id, provider, company, tenant, started_at, finished_at,
                latency_ms, jobs_received, jobs_new, jobs_duplicate, jobs_filtered, error_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry.get("cycle_id"), entry.get("provider", ""), entry.get("company", ""),
                entry.get("tenant", ""), entry.get("started_at"), entry.get("finished_at"),
                entry.get("latency_ms", 0.0), entry.get("jobs_received", 0), entry.get("jobs_new", 0),
                entry.get("jobs_duplicate", 0), entry.get("jobs_filtered", 0), entry.get("error_type", ""),
            ),
        )
        return cur.lastrowid


def list_discovery_log(limit: int = 50, provider: Optional[str] = None) -> list[dict]:
    query = "SELECT * FROM discovery_log"
    params: list = []
    if provider:
        query += " WHERE provider = ?"
        params.append(provider)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with db_session() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
