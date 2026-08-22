"""Live-queried optimizer metrics (CLAUDE.md Phase 14 section 67). No PII
labels, no in-memory counters -- every value is a fresh query over persisted
state, matching every other metrics module in this project."""

from app.db import db_session


def collect() -> dict:
    with db_session() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM resume_variants").fetchone()["c"]
        failed = conn.execute(
            "SELECT COUNT(*) AS c FROM resume_variants WHERE status IN ('CLAIM_CHECK_FAILED', 'ATS_PARSE_FAILED')"
        ).fetchone()["c"]
        ready = conn.execute("SELECT COUNT(*) AS c FROM resume_variants WHERE current = 1 AND status = 'READY'").fetchone()["c"]
        stale = conn.execute("SELECT COUNT(*) AS c FROM resume_variants WHERE current = 1 AND status = 'STALE'").fetchone()["c"]
        claim_failures = conn.execute(
            "SELECT COUNT(*) AS c FROM resume_variants WHERE status = 'CLAIM_CHECK_FAILED'"
        ).fetchone()["c"]
        parse_failures = conn.execute(
            "SELECT COUNT(*) AS c FROM resume_variants WHERE status = 'ATS_PARSE_FAILED'"
        ).fetchone()["c"]
        low_alignment = conn.execute(
            "SELECT COUNT(*) AS c FROM resume_quality_reports qr JOIN resume_variants rv "
            "ON rv.variant_id = qr.variant_id WHERE rv.current = 1 AND qr.alignment_label = 'LOW_ALIGNMENT'"
        ).fetchone()["c"]
        distribution_rows = conn.execute(
            "SELECT required_total, required_matched, required_transferable FROM resume_quality_reports qr "
            "JOIN resume_variants rv ON rv.variant_id = qr.variant_id WHERE rv.current = 1"
        ).fetchall()

    buckets = {"0-25%": 0, "25-50%": 0, "50-75%": 0, "75-100%": 0}
    for r in distribution_rows:
        total_req = r["required_total"] or 0
        if total_req == 0:
            continue
        ratio = (r["required_matched"] + 0.5 * r["required_transferable"]) / total_req
        pct = ratio * 100
        if pct < 25:
            buckets["0-25%"] += 1
        elif pct < 50:
            buckets["25-50%"] += 1
        elif pct < 75:
            buckets["50-75%"] += 1
        else:
            buckets["75-100%"] += 1

    return {
        "resume_optimizations_total": total,
        "resume_optimizations_failed": failed,
        "resume_variants_ready": ready,
        "resume_variants_stale": stale,
        "resume_claim_failures": claim_failures,
        "resume_parse_failures": parse_failures,
        "jobs_low_alignment": low_alignment,
        "required_skill_coverage_distribution": buckets,
    }
