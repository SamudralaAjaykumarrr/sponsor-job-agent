"""Workday tenant/site-specific routing and capability tracking (CLAUDE.md
Phase 11 sections 10-13, 45). Workday's real candidate-facing behavior
varies genuinely tenant-by-tenant (custom branding, different login/account
requirements, different question sets) -- this module never assumes one
tenant's observed behavior generalizes to "Workday is supported"; every
capability is recorded per (tenant, site) and the matrix/report functions
below always render per-row, never collapsed into one blanket claim.

A Workday candidate-facing URL has the shape:
  https://{tenant}.{wdHost}/{site}/job/{location}/{title}_{requisition_id}
e.g. https://acme.wd5.myworkdayjobs.com/External/job/Remote/Software-Engineer_R-1234

or the CXS API shape used by app.providers.workday:
  https://{tenant}.{wdHost}/wday/cxs/{tenant}/{site}/jobs
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from app.db import db_session

_CANDIDATE_URL_RE = re.compile(
    r"^(?P<tenant>[a-z0-9\-]+)\.(?P<wdhost>wd\d+\.myworkdayjobs\.com|wd\d+\.myworkdaysite\.com)$", re.I,
)
_SITE_FROM_PATH_RE = re.compile(r"^/(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[^/]+)/job/", re.I)
_CXS_PATH_RE = re.compile(r"^/wday/cxs/(?P<tenant>[^/]+)/(?P<site>[^/]+)/", re.I)
_REQUISITION_RE = re.compile(r"_(R-?\d+)$", re.I)


@dataclass
class WorkdayTenantInfo:
    tenant: str
    site: str
    host: str
    requisition_id: str = ""
    recognized: bool = False


def parse_workday_tenant(url: str) -> WorkdayTenantInfo:
    """Best-effort, safe parse of a real Workday URL into its tenant/site --
    never guesses a tenant that isn't actually present in the URL itself
    (CLAUDE.md Phase 11 section 10 'do not assume all Workday tenants behave
    alike' extends to never fabricating one either)."""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    match = _CANDIDATE_URL_RE.match(host)
    if not match:
        cxs = _CXS_PATH_RE.match(parsed.path or "")
        if cxs:
            return WorkdayTenantInfo(tenant=cxs.group("tenant"), site=cxs.group("site"), host=host, recognized=True)
        return WorkdayTenantInfo(tenant="", site="", host=host, recognized=False)

    tenant = match.group("tenant")
    site_match = _SITE_FROM_PATH_RE.match(parsed.path or "")
    if site_match:
        site = site_match.group("site")
    else:
        cxs_match = _CXS_PATH_RE.match(parsed.path or "")
        site = cxs_match.group("site") if cxs_match else ""
    requisition = ""
    req_match = _REQUISITION_RE.search(parsed.path or "")
    if req_match:
        requisition = req_match.group(1).upper()
    return WorkdayTenantInfo(tenant=tenant, site=site, host=host, requisition_id=requisition, recognized=True)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# Capability keys tracked per tenant/site -- CLAUDE.md Phase 11 section 13's
# exact list.
CAPABILITY_KEYS = (
    "landing_navigation", "login_required", "resume_upload", "profile_import",
    "multi_step", "custom_questions", "review_page", "confirmation_detection",
)


def record_observation(tenant: str, site: str, host: str, *, notes: str = "", **capabilities: Optional[bool]) -> dict:
    """Upserts one tenant/site's observed capabilities. Only keys explicitly
    passed are updated -- an unmentioned capability keeps its prior value
    (still NULL / not-yet-observed if this is the first observation) rather
    than being reset to a guess."""
    unknown_keys = set(capabilities) - set(CAPABILITY_KEYS)
    if unknown_keys:
        raise ValueError(f"unknown Workday capability key(s): {sorted(unknown_keys)}")

    now = utcnow()
    with db_session() as conn:
        existing = conn.execute(
            "SELECT * FROM workday_tenant_observations WHERE tenant = ? AND site = ?", (tenant, site),
        ).fetchone()
        if existing is None:
            columns = ["tenant", "site", "host", "notes", "observed_at", "updated_at"] + list(capabilities)
            values = [tenant, site, host, notes, now, now] + [
                (1 if v else 0) if v is not None else None for v in capabilities.values()
            ]
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(
                f"INSERT INTO workday_tenant_observations ({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )
        else:
            set_parts = ["host = ?", "observed_at = ?", "updated_at = ?"]
            set_values: list = [host, now, now]
            if notes:
                set_parts.append("notes = ?")
                set_values.append(notes)
            for key, value in capabilities.items():
                set_parts.append(f"{key} = ?")
                set_values.append((1 if value else 0) if value is not None else None)
            set_values.extend([tenant, site])
            conn.execute(
                f"UPDATE workday_tenant_observations SET {', '.join(set_parts)} WHERE tenant = ? AND site = ?",
                set_values,
            )
    return get_observation(tenant, site)


def get_observation(tenant: str, site: str) -> Optional[dict]:
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM workday_tenant_observations WHERE tenant = ? AND site = ?", (tenant, site),
        ).fetchone()
        return dict(row) if row else None


def list_observations(limit: int = 200) -> list[dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM workday_tenant_observations ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def render_tenant_matrix() -> str:
    """CLAUDE.md Phase 11 section 45: renders per-tenant rows, never a
    single collapsed 'Workday: supported' line."""
    rows = list_observations()
    if not rows:
        return "Workday Tenant Matrix\n" + "=" * 30 + "\nNo tenant/site has been observed yet.\n"
    lines = ["Workday Tenant Matrix", "=" * 30]
    for row in rows:
        lines.append(f"\nTenant: {row['tenant']}  Site: {row['site']}  Host: {row['host']}")
        for key in CAPABILITY_KEYS:
            value = row.get(key)
            rendered = "not observed" if value is None else bool(value)
            lines.append(f"  {key}: {rendered}")
        lines.append(f"  observed_at: {row['observed_at']}")
        if row.get("notes"):
            lines.append(f"  notes: {row['notes']}")
    return "\n".join(lines) + "\n"
