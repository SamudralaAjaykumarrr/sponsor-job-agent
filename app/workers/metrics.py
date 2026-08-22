"""Honest, DB-derived fleet/monitoring metrics -- CLAUDE.md Phase 5 sections
21/22/37: never conflate STORAGE SCALE ("the registry has N rows") with
ACTUAL MONITORED SCALE ("N portals were actually polled on schedule").
Every number here is a live query; nothing is estimated or extrapolated."""

from datetime import datetime, timedelta, timezone
from statistics import quantiles

from app.db import db_session
from app.registry import repo as registry_repo
from app.registry import store as registry_store
from app.registry.scheduling import FAILING_THRESHOLD, FAILURE_BACKOFF_THRESHOLD
from app.workers import repo as workers_repo


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _since(hours: float) -> str:
    return (utcnow() - timedelta(hours=hours)).isoformat()


def fleet_snapshot() -> dict:
    """The dashboard/CLI's single source of truth for "how much are we
    actually monitoring right now", distinct from how much is merely stored.
    See docs/scaling-claims.md for the wording policy this backs."""
    since_1h = _since(1)
    since_24h = _since(24)

    stored_companies = registry_store.count_companies()
    stored_portals = registry_store.count_portals()
    candidate_portals = registry_store.count_portals(verification_status="DISCOVERED") + \
        registry_store.count_portals(verification_status="CANDIDATE")
    verified_portals = registry_store.count_portals(verification_status="VERIFIED")
    active_portals = registry_store.count_portals(verification_status="ACTIVE")

    entries = registry_repo.list_entries(enabled_only=True)
    operational_targets = len(entries)
    healthy = sum(1 for e in entries if e.consecutive_failures < FAILURE_BACKOFF_THRESHOLD)
    failing = sum(1 for e in entries if e.consecutive_failures >= FAILING_THRESHOLD)

    polled_1h = workers_repo.distinct_polled_portal_ids_since(since_1h)
    polled_24h = workers_repo.distinct_polled_portal_ids_since(since_24h)
    enabled_ids = {e.id for e in entries}

    jobs_1h = workers_repo.sum_jobs_since(since_1h)
    jobs_24h = workers_repo.sum_jobs_since(since_24h)

    dead_letters_open = workers_repo.count_dead_letters(resolved=False)

    with db_session() as conn:
        circuit_open = conn.execute(
            "SELECT COUNT(*) AS c FROM provider_circuit_state WHERE state != 'CLOSED'"
        ).fetchone()["c"]
        new_jobs_1h = conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE first_seen_at >= ?", (since_1h,)
        ).fetchone()["c"]
        new_jobs_24h = conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE first_seen_at >= ?", (since_24h,)
        ).fetchone()["c"]

    covered = len(polled_24h & enabled_ids)
    coverage_24h = round(covered / operational_targets, 4) if operational_targets else None

    return {
        "stored_companies": stored_companies,
        "stored_portals": stored_portals,
        "candidate_portals": candidate_portals,
        "verified_portals": verified_portals,
        "active_portals": active_portals,
        "operational_poll_targets": operational_targets,
        "healthy_monitored_portals": healthy,
        "failing_monitored_portals": failing,
        "actually_polled_last_1h": len(polled_1h),
        "actually_polled_last_24h": len(polled_24h),
        "monitoring_coverage_24h": coverage_24h,
        "jobs_fetched_last_1h": jobs_1h["jobs_received"],
        "jobs_fetched_last_24h": jobs_24h["jobs_received"],
        "new_jobs_last_1h": new_jobs_1h,
        "new_jobs_last_24h": new_jobs_24h,
        "dead_letters_open": dead_letters_open,
        "provider_circuits_open_or_half_open": circuit_open,
    }


def discovery_latency_percentiles() -> dict:
    """discovery_latency = first_seen_at - published_at, computed ONLY for
    jobs whose published_at came from a real provider timestamp
    (freshness_source == 'PUBLISHED_AT') -- never for a fabricated/relative
    "posted 3 days ago" string, per CLAUDE.md Phase 5 section 22."""
    with db_session() as conn:
        rows = conn.execute(
            "SELECT published_at, first_seen_at FROM jobs WHERE freshness_source = 'PUBLISHED_AT' AND published_at IS NOT NULL"
        ).fetchall()

    deltas_minutes: list[float] = []
    for row in rows:
        try:
            published = datetime.fromisoformat(row["published_at"].replace("Z", "+00:00"))
            seen = datetime.fromisoformat(row["first_seen_at"])
        except (ValueError, AttributeError):
            continue
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        delta = (seen - published).total_seconds() / 60.0
        if delta >= 0:
            deltas_minutes.append(delta)

    if len(deltas_minutes) == 0:
        return {
            "sample_size": 0, "p50_minutes": None, "p90_minutes": None,
            "p95_minutes": None, "p99_minutes": None, "note": "no jobs with valid provider timestamps yet",
        }

    if len(deltas_minutes) == 1:
        only = round(deltas_minutes[0], 2)
        return {"sample_size": 1, "p50_minutes": only, "p90_minutes": only, "p95_minutes": only, "p99_minutes": only}

    deltas_minutes.sort()
    qs = quantiles(deltas_minutes, n=100, method="inclusive")

    def _pick(p: int) -> float:
        return round(qs[p - 1], 2)

    return {
        "sample_size": len(deltas_minutes),
        "p50_minutes": _pick(50),
        "p90_minutes": _pick(90),
        "p95_minutes": _pick(95),
        "p99_minutes": _pick(99) if len(deltas_minutes) >= 100 else round(max(deltas_minutes), 2),
    }
