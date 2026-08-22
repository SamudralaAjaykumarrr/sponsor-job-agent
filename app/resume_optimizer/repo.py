"""DB access for jd_analyses/jd_requirements/resume_variants/
resume_quality_reports/resume_evidence_links (CLAUDE.md Phase 14 section 70).
Follows the existing app.jobs_repo / app.applications.repo conventions:
`?` placeholders, explicit bool->int coercion, db_session() per call."""

import json
import sqlite3
import uuid
from typing import Optional

from app.db import db_session
from app.models import utcnow
from app.resume_optimizer.models import JDAnalysisResult, JDRequirementItem, RequirementMatch


def _coerce(v):
    if hasattr(v, "value"):
        return v.value
    if isinstance(v, bool):
        return int(v)
    return v


# --- jd_analyses / jd_requirements ------------------------------------------

def get_jd_analysis(job_id: int, jd_fingerprint: str) -> Optional[dict]:
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM jd_analyses WHERE job_id = ? AND jd_fingerprint = ?",
            (job_id, jd_fingerprint),
        ).fetchone()
        if not row:
            return None
        analysis = dict(row)
        for key in ("domain_signals", "responsibilities", "education_requirements", "certification_requirements"):
            try:
                analysis[key] = json.loads(analysis.get(key) or "[]")
            except (ValueError, TypeError):
                analysis[key] = []
        req_rows = conn.execute(
            "SELECT * FROM jd_requirements WHERE jd_analysis_id = ? ORDER BY id ASC", (analysis["id"],)
        ).fetchall()
        analysis["requirements"] = [dict(r) for r in req_rows]
        return analysis


def save_jd_analysis(job_id: int, jd_fingerprint: str, analysis: JDAnalysisResult) -> int:
    """Idempotent AND concurrency-safe: a jd_analyses row already existing
    for (job_id, jd_fingerprint) is returned unchanged rather than
    duplicated (CLAUDE.md section 58's caching principle applied to JD
    analysis too). The initial existence check is a fast path only -- the
    actual guard against two concurrent callers both inserting is the
    unique index (idx_jd_analyses_job_fingerprint) plus the
    catch-and-refetch below, not the check itself (a real UniqueViolation
    race was caught live by this phase's own concurrent-Postgres
    validation: two threads both passing the initial existence check before
    either committed its INSERT)."""
    existing = get_jd_analysis(job_id, jd_fingerprint)
    if existing:
        return existing["id"]

    try:
        with db_session() as conn:
            cur = conn.execute(
                """INSERT INTO jd_analyses
                   (job_id, jd_fingerprint, analyzer_version, job_title, seniority, required_years,
                    domain_signals, responsibilities, education_requirements, certification_requirements,
                    sponsorship_language_present, salary_mentioned, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id, jd_fingerprint, analysis.analyzer_version, analysis.job_title, analysis.seniority,
                    analysis.required_years, json.dumps(analysis.domain_signals), json.dumps(analysis.responsibilities),
                    json.dumps(analysis.education_requirements), json.dumps(analysis.certification_requirements),
                    _coerce(analysis.sponsorship_language_present), _coerce(analysis.salary_mentioned), utcnow(),
                ),
            )
            analysis_id = cur.lastrowid
            for req in analysis.requirements:
                conn.execute(
                    """INSERT INTO jd_requirements
                       (jd_analysis_id, text, normalized_value, category, priority, evidence_span,
                        confidence, negated, conditional)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        analysis_id, req.text, req.normalized_value, _coerce(req.category), _coerce(req.priority),
                        req.evidence_span, req.confidence, _coerce(req.negated), _coerce(req.conditional),
                    ),
                )
        return analysis_id
    except sqlite3.IntegrityError:
        existing = get_jd_analysis(job_id, jd_fingerprint)
        if existing:
            return existing["id"]
        raise
    except Exception as exc:  # noqa: BLE001 -- psycopg's own IntegrityError subclass
        if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
            existing = get_jd_analysis(job_id, jd_fingerprint)
            if existing:
                return existing["id"]
        raise


def requirements_from_rows(rows: list[dict]) -> list[JDRequirementItem]:
    from app.resume_optimizer.models import RequirementCategory, RequirementPriority

    return [
        JDRequirementItem(
            text=r["text"], normalized_value=r["normalized_value"], category=RequirementCategory(r["category"]),
            priority=RequirementPriority(r["priority"]), evidence_span=r["evidence_span"] or "",
            confidence=r["confidence"] or 1.0, negated=bool(r["negated"]), conditional=bool(r["conditional"]),
        )
        for r in rows
    ]


# --- resume_variants ---------------------------------------------------------

def get_variant_by_identity(job_id: int, jd_fingerprint: str, profile_version: str, optimizer_version: str) -> Optional[dict]:
    with db_session() as conn:
        row = conn.execute(
            """SELECT * FROM resume_variants WHERE job_id = ? AND jd_fingerprint = ?
               AND profile_version = ? AND optimizer_version = ?""",
            (job_id, jd_fingerprint, profile_version, optimizer_version),
        ).fetchone()
        return dict(row) if row else None


def get_current_variant(job_id: int) -> Optional[dict]:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM resume_variants WHERE job_id = ? AND current = 1", (job_id,)).fetchone()
        return dict(row) if row else None


class DuplicateVariantError(Exception):
    """Raised when a concurrent caller already claimed the identical
    (job_id, jd_fingerprint, profile_version, optimizer_version) row --
    CLAUDE.md section 72's concurrency guard, enforced by the database's own
    unique index rather than an app-level check-then-insert."""


def claim_variant(job_id: int, jd_fingerprint: str, profile_version: str, optimizer_version: str) -> dict:
    """Atomically claims (creates) the GENERATING placeholder row for this
    exact identity. On a unique-constraint violation (another
    thread/process/worker already claimed it), raises DuplicateVariantError
    -- the caller should re-fetch via get_variant_by_identity instead of
    generating a second time."""
    variant_id = uuid.uuid4().hex
    now = utcnow()
    with db_session() as conn:
        try:
            conn.execute(
                """INSERT INTO resume_variants
                   (variant_id, job_id, jd_fingerprint, profile_version, optimizer_version,
                    status, current, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'GENERATING', 0, ?, ?)""",
                (variant_id, job_id, jd_fingerprint, profile_version, optimizer_version, now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateVariantError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 -- psycopg raises its own IntegrityError subclass
            if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                raise DuplicateVariantError(str(exc)) from exc
            raise
    return {"variant_id": variant_id, "job_id": job_id, "jd_fingerprint": jd_fingerprint,
            "profile_version": profile_version, "optimizer_version": optimizer_version, "status": "GENERATING"}


def finalize_variant(
    variant_id: str, *, status: str, resume_docx_path: str, resume_pdf_path: str, resume_txt_path: str,
    resume_artifact_hash: str, make_current: bool,
) -> None:
    """Marks one variant READY/CLAIM_CHECK_FAILED/ATS_PARSE_FAILED and, if
    make_current, atomically demotes any prior current variant for the same
    job first -- so the partial unique index on (job_id) WHERE current = 1
    is never violated by two rows racing to become current."""
    now = utcnow()
    with db_session() as conn:
        row = conn.execute("SELECT job_id FROM resume_variants WHERE variant_id = ?", (variant_id,)).fetchone()
        if row is None:
            return
        job_id = row["job_id"]
        if make_current:
            conn.execute(
                """UPDATE resume_variants SET current = 0, updated_at = ?,
                   status = CASE WHEN status = 'READY' THEN 'STALE' ELSE status END
                   WHERE job_id = ? AND current = 1 AND variant_id != ?""",
                (now, job_id, variant_id),
            )
        conn.execute(
            """UPDATE resume_variants SET status = ?, current = ?, resume_docx_path = ?, resume_pdf_path = ?,
               resume_txt_path = ?, resume_artifact_hash = ?, updated_at = ? WHERE variant_id = ?""",
            (status, _coerce(make_current), resume_docx_path, resume_pdf_path, resume_txt_path,
             resume_artifact_hash, now, variant_id),
        )


def mark_stale(job_id: int) -> None:
    """CLAUDE.md sections 36, 59: called when a job's JD or the candidate
    profile changes materially -- the current variant is marked STALE
    (never silently kept as if nothing changed) but stays `current` until a
    fresh generation supersedes it, so the dashboard/executor can still see
    what it was."""
    with db_session() as conn:
        conn.execute(
            "UPDATE resume_variants SET status = 'STALE', updated_at = ? WHERE job_id = ? AND current = 1 AND status = 'READY'",
            (utcnow(), job_id),
        )


def get_current_variants_for_jobs(job_ids: list[int]) -> dict[int, dict]:
    """Batched current-variant lookup for dashboard list rendering (CLAUDE.md
    section 55) -- one query for N jobs, never N queries."""
    if not job_ids:
        return {}
    placeholders = ",".join("?" for _ in job_ids)
    with db_session() as conn:
        rows = conn.execute(
            f"SELECT * FROM resume_variants WHERE job_id IN ({placeholders}) AND current = 1", job_ids
        ).fetchall()
        return {r["job_id"]: dict(r) for r in rows}


def list_variants_for_job(job_id: int) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM resume_variants WHERE job_id = ? ORDER BY created_at DESC", (job_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# --- resume_quality_reports ---------------------------------------------------

def save_quality_report(variant_id: str, job_id: int, jd_fingerprint: str, report_dict: dict) -> None:
    req_cov = report_dict["required_skill_coverage"]
    pref_cov = report_dict["preferred_skill_coverage"]
    with db_session() as conn:
        conn.execute(
            """INSERT INTO resume_quality_reports
               (variant_id, job_id, jd_fingerprint, resume_artifact_hash, required_total, required_matched,
                required_transferable, preferred_total, preferred_matched, ats_parseability, alignment_label,
                internal_alignment_score, claim_check_passed, optimizer_version, quality_version, report_json,
                generated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (variant_id) DO UPDATE SET
                 report_json = excluded.report_json, alignment_label = excluded.alignment_label,
                 internal_alignment_score = excluded.internal_alignment_score,
                 claim_check_passed = excluded.claim_check_passed, ats_parseability = excluded.ats_parseability,
                 generated_at = excluded.generated_at""",
            (
                variant_id, job_id, jd_fingerprint, report_dict["resume_artifact_hash"],
                req_cov["total"], req_cov["directly_verified"], req_cov["transferable"],
                pref_cov["total"], pref_cov["directly_verified"],
                report_dict["ats_parseability"]["overall"], report_dict["alignment_label"],
                report_dict["internal_alignment_score"], _coerce(report_dict["claim_check"]["passed"]),
                report_dict["optimizer_version"], report_dict["quality_version"],
                json.dumps(report_dict), report_dict["generated_at"],
            ),
        )


def get_quality_report(variant_id: str) -> Optional[dict]:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM resume_quality_reports WHERE variant_id = ?", (variant_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["report"] = json.loads(d["report_json"])
        return d


def get_quality_report_for_job(job_id: int) -> Optional[dict]:
    """Current-variant convenience lookup used by the dashboard pipeline
    table (CLAUDE.md sections 45-46, 55) -- one indexed join, no JD/resume
    recomputation."""
    with db_session() as conn:
        row = conn.execute(
            """SELECT qr.* FROM resume_quality_reports qr
               JOIN resume_variants rv ON rv.variant_id = qr.variant_id
               WHERE rv.job_id = ? AND rv.current = 1""",
            (job_id,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["report"] = json.loads(d["report_json"])
        return d


def get_quality_reports_for_jobs(job_ids: list[int]) -> dict[int, dict]:
    """Batched version of get_quality_report_for_job for dashboard list
    rendering -- one query for N jobs, never N queries (CLAUDE.md section 55)."""
    if not job_ids:
        return {}
    placeholders = ",".join("?" for _ in job_ids)
    with db_session() as conn:
        rows = conn.execute(
            f"""SELECT qr.* FROM resume_quality_reports qr
                JOIN resume_variants rv ON rv.variant_id = qr.variant_id
                WHERE rv.job_id IN ({placeholders}) AND rv.current = 1""",
            job_ids,
        ).fetchall()
        out = {}
        for row in rows:
            d = dict(row)
            d["report"] = json.loads(d["report_json"])
            out[d["job_id"]] = d
        return out


# --- resume_evidence_links ----------------------------------------------------

def save_evidence_links(variant_id: str, matches: list[RequirementMatch]) -> None:
    with db_session() as conn:
        for m in matches:
            conn.execute(
                """INSERT INTO resume_evidence_links
                   (variant_id, requirement_text, requirement_category, requirement_priority, status,
                    evidence_ids, explanation)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    variant_id, m.requirement.text, _coerce(m.requirement.category), _coerce(m.requirement.priority),
                    _coerce(m.status), json.dumps(m.evidence_ids), m.explanation,
                ),
            )


def list_evidence_links(variant_id: str) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM resume_evidence_links WHERE variant_id = ? ORDER BY id ASC", (variant_id,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["evidence_ids"] = json.loads(d["evidence_ids"] or "[]")
            out.append(d)
        return out
