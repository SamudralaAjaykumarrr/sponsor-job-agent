"""Production observability metrics abstraction (CLAUDE.md Phase 6 sections
29-30). Every value here is computed live from the database at request time
-- nothing is accumulated in-process (which would be wrong the moment more
than one worker/dashboard process exists) and nothing is estimated or
extrapolated. Exposed both as a plain dict (for /fleet/metrics JSON, already
existing) and rendered as Prometheus text exposition format at /metrics.

Deliberately does NOT add the `prometheus_client` dependency: every metric
here is a live gauge freshly queried from the DB on each scrape (never an
in-process counter/histogram accumulated between scrapes), so
prometheus_client's core value -- managing that in-process accumulation --
doesn't fit this model. A small, dependency-free text-format renderer is
simpler and just as correct for gauges. If a future phase adds true
in-process counters (e.g. per-request timing histograms), reconsider then.

Never exposes candidate PII -- every metric here is fleet/provider/queue
shaped, never job or candidate content."""

from datetime import datetime, timezone

from app.db import backend as db_backend
from app.db import db_session
from app.workers import leasing as leasing_mod
from app.workers import repo as workers_repo
from app.workers import schema_drift_repo
from app.workers.metrics import discovery_latency_percentiles, fleet_snapshot

_METRIC_PREFIX = "sponsor_job_agent"

# CircuitState enum values mapped to a small integer for a Prometheus gauge.
_CIRCUIT_STATE_CODE = {"CLOSED": 0, "HALF_OPEN": 1, "OPEN": 2}


def _queue_depth() -> dict:
    """Due-but-not-currently-leased rows in each queue -- a real backlog
    signal distinct from `operational_poll_targets` (which counts ALL
    enabled targets regardless of whether they're due right now)."""
    now = datetime.now(timezone.utc).isoformat()
    with db_session() as conn:
        poll_depth = conn.execute(
            """SELECT COUNT(*) AS c FROM company_registry
               WHERE enabled = 1 AND (next_poll_at IS NULL OR next_poll_at <= ?)
                 AND (lease_expires_at IS NULL OR lease_expires_at <= ?)""",
            (now, now),
        ).fetchone()["c"]
        verification_depth = conn.execute(
            """SELECT COUNT(*) AS c FROM registry_portals
               WHERE enabled = 1 AND verification_status IN ('DISCOVERED', 'CANDIDATE')
                 AND (verify_lease_expires_at IS NULL OR verify_lease_expires_at <= ?)""",
            (now,),
        ).fetchone()["c"]
    return {"poll_queue_depth": poll_depth, "verification_queue_depth": verification_depth}


def _retry_depth() -> int:
    """Portals currently in a backoff/retry state (at least one recorded
    consecutive failure) -- distinct from dead-lettered (which have given up
    entirely)."""
    with db_session() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM company_registry WHERE enabled = 1 AND consecutive_failures > 0"
        ).fetchone()
        return row["c"]


def provider_circuit_states() -> dict[str, str]:
    with db_session() as conn:
        rows = conn.execute("SELECT provider, state FROM provider_circuit_state").fetchall()
        return {r["provider"]: r["state"] for r in rows}


def _provider_failure_counts(since_hours: float = 1.0) -> dict[str, int]:
    from datetime import timedelta

    since = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
    with db_session() as conn:
        rows = conn.execute(
            "SELECT provider, COUNT(*) AS c FROM poll_attempts "
            "WHERE started_at >= ? AND status IN ('RETRYABLE_FAILURE', 'PERMANENT_FAILURE') "
            "GROUP BY provider",
            (since,),
        ).fetchall()
        return {r["provider"]: r["c"] for r in rows}


def _provider_rate_limit_counts(since_hours: float = 1.0) -> dict[str, int]:
    from datetime import timedelta

    since = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
    with db_session() as conn:
        rows = conn.execute(
            "SELECT provider, COUNT(*) AS c FROM poll_attempts "
            "WHERE started_at >= ? AND error_type = 'RATE_LIMITED' GROUP BY provider",
            (since,),
        ).fetchall()
        return {r["provider"]: r["c"] for r in rows}


def collect() -> dict:
    """Single source of truth for every scalar/labeled metric this app
    exposes. Returns a plain dict -- render_prometheus_text() below turns it
    into Prometheus exposition format; /fleet/metrics returns it as JSON
    directly."""
    snapshot = fleet_snapshot()
    latency = discovery_latency_percentiles()
    workers = workers_repo.list_workers(limit=1000)
    online_statuses = {"STARTING", "IDLE", "WORKING", "DEGRADED"}
    workers_online = sum(1 for w in workers if w["status"] in online_statuses)
    workers_offline = sum(1 for w in workers if w["status"] in ("STOPPED", "OFFLINE"))

    queue_depth = _queue_depth()

    from app.agent import metrics as agent_metrics
    from app.applications import budget as application_budget
    from app.applications import metrics as application_metrics
    from app.sponsorship import metrics as sponsorship_metrics

    return {
        **sponsorship_metrics.collect(),
        **application_metrics.collect(),
        **application_metrics.collect_worker_fleet(),
        **application_budget.collect().as_dict(),
        **agent_metrics.collect(),
        "database_backend": db_backend(),
        "workers_online": workers_online,
        "workers_offline": workers_offline,
        "leases_active_poll": leasing_mod.count_active_poll_leases(),
        "leases_active_verification": leasing_mod.count_active_verification_leases(),
        "attempts_total_1h": snapshot["actually_polled_last_1h"],
        "attempts_failed_1h": sum(_provider_failure_counts(since_hours=1.0).values()),
        "jobs_fetched_total_1h": snapshot["jobs_fetched_last_1h"],
        "jobs_new_total_1h": snapshot["new_jobs_last_1h"],
        "portals_active": snapshot["active_portals"],
        "portals_polled_1h": snapshot["actually_polled_last_1h"],
        "portals_polled_24h": snapshot["actually_polled_last_24h"],
        "monitoring_coverage_24h": snapshot["monitoring_coverage_24h"],
        "poll_queue_depth": queue_depth["poll_queue_depth"],
        "verification_queue_depth": queue_depth["verification_queue_depth"],
        "retry_depth": _retry_depth(),
        "dead_letter_count": workers_repo.count_dead_letters(resolved=False),
        "schema_drift_events_total": len(schema_drift_repo.list_recent_drift(limit=10_000)),
        "provider_circuit_state": provider_circuit_states(),
        "provider_failures_1h": _provider_failure_counts(since_hours=1.0),
        "provider_rate_limits_1h": _provider_rate_limit_counts(since_hours=1.0),
        "discovery_latency_p50_minutes": latency["p50_minutes"],
        "discovery_latency_p90_minutes": latency["p90_minutes"],
        "discovery_latency_p95_minutes": latency["p95_minutes"],
        "discovery_latency_p99_minutes": latency["p99_minutes"],
        "discovery_latency_sample_size": latency["sample_size"],
    }


def render_prometheus_text(metrics: dict) -> str:
    lines: list[str] = []

    def _gauge(name: str, value, labels: str = "") -> None:
        if value is None:
            return
        full_name = f"{_METRIC_PREFIX}_{name}"
        lines.append(f"# TYPE {full_name} gauge")
        lines.append(f"{full_name}{labels} {value}")

    for key, value in metrics.items():
        if key in ("database_backend", "agent_actual_state"):
            continue
        if isinstance(value, dict):
            if key in ("provider_circuit_state", "application_provider_circuit_state"):
                for provider, state in value.items():
                    _gauge(key, _CIRCUIT_STATE_CODE.get(state, -1), labels=f'{{provider="{provider}"}}')
            elif key == "sponsorship_decisions_total":
                for status, count in value.items():
                    _gauge(key, count, labels=f'{{status="{status}"}}')
            else:
                for provider, count in value.items():
                    _gauge(key, count, labels=f'{{provider="{provider}"}}')
        else:
            _gauge(key, value)

    return "\n".join(lines) + "\n"
