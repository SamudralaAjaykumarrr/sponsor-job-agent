"""Sponsorship intelligence observability metrics (CLAUDE.md Phase 7 section
49). Every value is a live DB query at collection time -- same pattern as
app.observability.metrics. Never exposes candidate PII (no job titles,
descriptions, or contact info -- only counts)."""

from app.db import db_session


def collect() -> dict:
    with db_session() as conn:
        evidence_records = conn.execute("SELECT COUNT(*) AS c FROM employer_sponsorship_evidence").fetchone()["c"]
        datasets_loaded = conn.execute(
            "SELECT COUNT(*) AS c FROM sponsorship_datasets WHERE status = 'COMPLETED'"
        ).fetchone()["c"]
        companies_recent = conn.execute(
            "SELECT COUNT(*) AS c FROM employer_sponsorship_profile WHERE historical_strength IN "
            "('STRONG_RECENT', 'SOME')"
        ).fetchone()["c"]
        decisions_by_status = {
            r["status"]: r["c"] for r in conn.execute(
                "SELECT status, COUNT(*) AS c FROM sponsorship_decisions GROUP BY status"
            ).fetchall()
        }
        conflicts_total = conn.execute(
            "SELECT COUNT(*) AS c FROM sponsorship_decisions WHERE conflicts != '[]'"
        ).fetchone()["c"]
        identity_ambiguous_total = conn.execute(
            "SELECT COUNT(*) AS c FROM employer_identity_review WHERE status = 'PENDING'"
        ).fetchone()["c"]
        review_queue_depth = conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE sponsorship_status = 'LIKELY_SPONSOR'"
        ).fetchone()["c"]

    return {
        "sponsorship_evidence_records": evidence_records,
        "sponsorship_datasets_loaded": datasets_loaded,
        "companies_with_recent_h1b_history": companies_recent,
        "sponsorship_decisions_total": decisions_by_status,
        "sponsorship_conflicts_total": conflicts_total,
        "identity_ambiguous_total": identity_ambiguous_total,
        "sponsorship_review_queue_depth": review_queue_depth,
    }
