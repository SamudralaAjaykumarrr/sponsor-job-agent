"""Real, DB-derived registry analytics -- every number here is a live COUNT
query against the actual registry tables, never a fabricated/estimated
figure. See CLAUDE.md Phase 4 sections 25, 31."""

from app.db import db_session


def snapshot() -> dict:
    """Deterministic top-level counts -- 'Companies: N / Portals: N / ...'."""
    with db_session() as conn:
        companies = conn.execute("SELECT COUNT(*) AS c FROM registry_companies").fetchone()["c"]
        portals = conn.execute("SELECT COUNT(*) AS c FROM registry_portals").fetchone()["c"]

        def count_status(*statuses: str) -> int:
            placeholders = ", ".join("?" for _ in statuses)
            return conn.execute(
                f"SELECT COUNT(*) AS c FROM registry_portals WHERE verification_status IN ({placeholders})",
                statuses,
            ).fetchone()["c"]

        verified = count_status("VERIFIED", "ACTIVE")
        active = count_status("ACTIVE")
        candidate = count_status("CANDIDATE", "DISCOVERED")
        quarantined = count_status("QUARANTINED")
        stale = count_status("STALE")
        degraded = count_status("DEGRADED")

        healthy = conn.execute(
            "SELECT COUNT(*) AS c FROM registry_portals WHERE verification_status = 'ACTIVE' AND consecutive_failures < 3"
        ).fetchone()["c"]

    return {
        "companies": companies, "portals": portals, "verified": verified, "active": active,
        "healthy": healthy, "candidate": candidate, "quarantined": quarantined, "stale": stale,
        "degraded": degraded,
    }


def provider_breakdown() -> list[dict]:
    """Per-provider counts: companies, active portals, healthy portals,
    total jobs seen (current_job_count sum), error rate proxy."""
    with db_session() as conn:
        rows = conn.execute(
            """SELECT
                 provider,
                 COUNT(DISTINCT company_id) AS companies,
                 SUM(CASE WHEN verification_status = 'ACTIVE' THEN 1 ELSE 0 END) AS active_portals,
                 SUM(CASE WHEN verification_status = 'ACTIVE' AND consecutive_failures < 3 THEN 1 ELSE 0 END) AS healthy_portals,
                 SUM(current_job_count) AS jobs_seen,
                 SUM(CASE WHEN consecutive_failures >= 3 THEN 1 ELSE 0 END) AS failing_portals,
                 COUNT(*) AS total_portals
               FROM registry_portals
               WHERE provider != ''
               GROUP BY provider
               ORDER BY provider ASC"""
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        total = d["total_portals"] or 1
        d["error_rate"] = round((d["failing_portals"] or 0) / total, 4)
        result.append(d)
    return result


def support_level_breakdown() -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT support_level, COUNT(*) AS c FROM registry_portals GROUP BY support_level ORDER BY support_level"
        ).fetchall()
        return [dict(r) for r in rows]
