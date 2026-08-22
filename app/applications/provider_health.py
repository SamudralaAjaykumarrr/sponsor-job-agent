"""Application/browser-assist PROVIDER FLOW health (CLAUDE.md Phase 13
sections 11-12, 15-16). Deliberately separate from:

  - `app.workers.circuit` (discovery-poll circuit breaker)
  - `app.applications.circuit` (application-SUBMISSION circuit breaker)

Both of those answer "should we keep hitting this provider's network
endpoint right now." This module answers a different question: "has this
provider's real-browser ASSIST flow (form discovery/fill) recently proven
itself trustworthy, or is it currently degraded/blocked/drifted." Recording
evidence here NEVER auto-disables anything -- a DEGRADED/STALE/SCHEMA_DRIFT
health only ever surfaces for review (doctor/dashboard), matching every
other capability-tracking module in this project (CLAUDE.md Phase 11 section
43's "never auto-disable a known-safe capability", extended here)."""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from app import config
from app.db import db_session


class ProviderAssistHealth(str, Enum):
    """CLAUDE.md Phase 13 section 11's exact vocabulary."""
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    VARIABLE = "VARIABLE"
    STALE = "STALE"
    CAPTCHA_BLOCKED = "CAPTCHA_BLOCKED"
    AUTH_GATED = "AUTH_GATED"
    SCHEMA_DRIFT = "SCHEMA_DRIFT"
    UNVERIFIED = "UNVERIFIED"
    UNSUPPORTED = "UNSUPPORTED"


# CLAUDE.md Phase 13 section 15: repeated consecutive failures downgrade a
# provider from HEALTHY to DEGRADED -- reused, never a second, drifting
# threshold, matching this project's existing consecutive-failure gates
# (app.workers.circuit / app.applications.circuit's own thresholds, which are
# submission/poll-shaped and therefore kept as separate config values).
DEGRADED_CONSECUTIVE_FAILURE_THRESHOLD = 3
SCHEMA_DRIFT_COUNT_THRESHOLD = 2


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _key(provider: str, tenant: str, site: str) -> tuple[str, str, str]:
    return (provider or "").lower(), tenant or "", site or ""


def _get_row(conn, provider: str, tenant: str, site: str) -> Optional[dict]:
    p, t, s = _key(provider, tenant, site)
    row = conn.execute(
        "SELECT * FROM application_provider_health WHERE provider = ? AND tenant = ? AND site = ?", (p, t, s),
    ).fetchone()
    return dict(row) if row else None


def _upsert(conn, provider: str, tenant: str, site: str, **fields) -> None:
    p, t, s = _key(provider, tenant, site)
    now = utcnow()
    existing = _get_row(conn, p, t, s)
    if existing is None:
        columns = ["provider", "tenant", "site", "created_at", "updated_at"] + list(fields)
        values = [p, t, s, now, now] + list(fields.values())
        placeholders = ", ".join("?" for _ in columns)
        conn.execute(
            f"INSERT INTO application_provider_health ({', '.join(columns)}) VALUES ({placeholders})", values,
        )
    else:
        set_parts = ["updated_at = ?"]
        set_values: list = [now]
        for key, value in fields.items():
            set_parts.append(f"{key} = ?")
            set_values.append(value)
        set_values.extend([p, t, s])
        conn.execute(
            f"UPDATE application_provider_health SET {', '.join(set_parts)} "
            "WHERE provider = ? AND tenant = ? AND site = ?", set_values,
        )


def record_success(provider: str, *, tenant: str = "", site: str = "", form_fingerprint: str = "",
                    live_validation: bool = False) -> dict:
    """A real-browser pass reached a fillable form (or further) with no
    CAPTCHA/auth-gate/schema-drift pause. Resets consecutive_failures to 0 --
    a fresh success is real evidence the flow currently works, even if it
    failed before (CLAUDE.md Phase 13 section 21's 'do not claim causality/
    do not cherry-pick' is about VARIABLE classification, not about refusing
    to record a genuine improvement)."""
    now = utcnow()
    with db_session() as conn:
        fields = {"last_success": now, "consecutive_failures": 0, "form_verified": 1}
        if form_fingerprint:
            fields["form_fingerprint"] = form_fingerprint
        if live_validation:
            fields["last_live_validation"] = now
        _upsert(conn, provider, tenant, site, **fields)
        return _get_row(conn, provider, tenant, site)


class FailureKind(str, Enum):
    GENERIC = "GENERIC"
    SCHEMA_DRIFT = "SCHEMA_DRIFT"
    CAPTCHA = "CAPTCHA"
    AUTH_GATE = "AUTH_GATE"


def record_failure(provider: str, kind: FailureKind = FailureKind.GENERIC, *, tenant: str = "", site: str = "") -> dict:
    now = utcnow()
    with db_session() as conn:
        existing = _get_row(conn, provider, tenant, site)
        consecutive = (existing.get("consecutive_failures") or 0) + 1 if existing else 1
        fields: dict = {"last_failure": now, "consecutive_failures": consecutive}
        if kind == FailureKind.SCHEMA_DRIFT:
            fields["schema_drift_count"] = ((existing.get("schema_drift_count") or 0) if existing else 0) + 1
        elif kind == FailureKind.CAPTCHA:
            fields["captcha_observed"] = 1
        elif kind == FailureKind.AUTH_GATE:
            fields["auth_gate_observed"] = 1
        _upsert(conn, provider, tenant, site, **fields)
        return _get_row(conn, provider, tenant, site)


def clear_captcha_flag(provider: str, *, tenant: str = "", site: str = "") -> dict:
    """A subsequent success genuinely clears a previously-observed CAPTCHA
    flag -- CAPTCHA presence is a per-visit fact, not a permanent scar, but
    only ever cleared by a real recorded success, never by silent timeout."""
    with db_session() as conn:
        _upsert(conn, provider, tenant, site, captcha_observed=0, auth_gate_observed=0)
        return _get_row(conn, provider, tenant, site)


def compute_health(row: Optional[dict], *, max_age_days: Optional[int] = None) -> ProviderAssistHealth:
    """CLAUDE.md Phase 13 sections 11, 15-16: deterministic classification
    from the row alone -- never a guess, never auto-inflated. Order matters:
    a captcha/auth-gate observation not yet cleared by a later success is the
    most actionable fact, checked first; staleness always wins over an
    otherwise-good-looking row (matches capability_evidence's own 'staleness
    always wins' rule); schema drift and repeated failure are checked next;
    HEALTHY is the last, most-demanding condition."""
    if row is None:
        return ProviderAssistHealth.UNVERIFIED
    if row.get("captcha_observed"):
        return ProviderAssistHealth.CAPTCHA_BLOCKED
    if row.get("auth_gate_observed"):
        return ProviderAssistHealth.AUTH_GATED
    max_age = max_age_days if max_age_days is not None else config.CAPABILITY_EVIDENCE_MAX_AGE_DAYS
    last_validation = row.get("last_live_validation") or row.get("last_success")
    if last_validation:
        try:
            observed = datetime.fromisoformat(last_validation)
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - observed).total_seconds() / 86400.0
            if age_days > max_age:
                return ProviderAssistHealth.STALE
        except ValueError:
            return ProviderAssistHealth.STALE
    else:
        return ProviderAssistHealth.UNVERIFIED
    if (row.get("schema_drift_count") or 0) >= SCHEMA_DRIFT_COUNT_THRESHOLD:
        return ProviderAssistHealth.SCHEMA_DRIFT
    if (row.get("consecutive_failures") or 0) >= DEGRADED_CONSECUTIVE_FAILURE_THRESHOLD:
        return ProviderAssistHealth.DEGRADED
    if row.get("form_verified") and row.get("last_success"):
        return ProviderAssistHealth.HEALTHY
    return ProviderAssistHealth.UNVERIFIED


def get_health(provider: str, *, tenant: str = "", site: str = "") -> dict:
    with db_session() as conn:
        row = _get_row(conn, provider, tenant, site)
    return {"row": row, "health": compute_health(row).value}


def list_health() -> list[dict]:
    """CLAUDE.md Phase 13 section 12: rendered per (provider, tenant, site)
    row, never collapsed into one blanket per-provider claim."""
    with db_session() as conn:
        rows = conn.execute("SELECT * FROM application_provider_health ORDER BY provider, tenant, site").fetchall()
    return [{"row": dict(r), "health": compute_health(dict(r)).value} for r in rows]


@dataclass
class ProviderHealthSummary:
    provider: str
    tenant: str
    site: str
    health: ProviderAssistHealth


def render_health_report() -> str:
    rows = list_health()
    if not rows:
        return "Application Provider Health\n" + "=" * 30 + "\nNo provider has been observed yet.\n"
    lines = ["Application Provider Health", "=" * 30]
    for entry in rows:
        r = entry["row"]
        label = f"{r['provider']}" + (f" / {r['tenant']}" if r["tenant"] else "") + (f" / {r['site']}" if r["site"] else "")
        lines.append(f"\n{label}: {entry['health']}")
        lines.append(f"  last_success={r.get('last_success') or 'never'}  last_failure={r.get('last_failure') or 'never'}")
        lines.append(f"  consecutive_failures={r.get('consecutive_failures') or 0}  "
                      f"schema_drift_count={r.get('schema_drift_count') or 0}")
    return "\n".join(lines) + "\n"
