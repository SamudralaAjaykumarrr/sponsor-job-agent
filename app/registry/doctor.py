"""Registry integrity checker ("registry doctor"). Read-only: reports
problems, never silently repairs them. `python -m app.registry.cli doctor`
exits nonzero when any SERIOUS issue is found. See CLAUDE.md Phase 4 section 30."""

from dataclasses import dataclass, field

from app.db import db_session
from app.providers.capabilities import SupportLevel
from app.registry.url_canon import is_valid_http_url


@dataclass
class Issue:
    severity: str  # "serious" | "warning"
    check: str
    detail: str


@dataclass
class DoctorReport:
    issues: list[Issue] = field(default_factory=list)

    @property
    def serious_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "serious")

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "warning")

    def as_dict(self) -> dict:
        return {
            "serious_count": self.serious_count,
            "warning_count": self.warning_count,
            "issues": [{"severity": i.severity, "check": i.check, "detail": i.detail} for i in self.issues],
        }


def run_doctor() -> DoctorReport:
    report = DoctorReport()
    with db_session() as conn:
        _check_duplicate_canonical_portals(conn, report)
        _check_active_missing_tenant(conn, report)
        _check_verified_without_provenance(conn, report)
        _check_invalid_urls(conn, report)
        _check_unsupported_marked_active(conn, report)
        _check_orphan_provenance(conn, report)
        _check_contradictory_domain_mappings(conn, report)
        _check_impossible_scheduler_state(conn, report)
    return report


def _check_duplicate_canonical_portals(conn, report: DoctorReport) -> None:
    rows = conn.execute(
        """SELECT canonical_url, COUNT(*) AS n FROM registry_portals
           WHERE canonical_url != '' AND enabled = 1
           GROUP BY canonical_url HAVING n > 1"""
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "duplicate_canonical_portal",
                                    f"{r['n']} enabled portals share canonical_url '{r['canonical_url']}'"))


def _check_active_missing_tenant(conn, report: DoctorReport) -> None:
    rows = conn.execute(
        "SELECT id, provider FROM registry_portals WHERE verification_status IN ('ACTIVE','VERIFIED') AND (tenant_identifier IS NULL OR tenant_identifier = '')"
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "active_missing_tenant",
                                    f"portal id={r['id']} provider={r['provider']} is ACTIVE/VERIFIED with no tenant_identifier"))


def _check_verified_without_provenance(conn, report: DoctorReport) -> None:
    rows = conn.execute(
        """SELECT rp.id, rp.provider FROM registry_portals rp
           LEFT JOIN registry_provenance prov ON prov.portal_id = rp.id
           WHERE rp.verification_status IN ('VERIFIED','ACTIVE') AND prov.id IS NULL"""
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "verified_without_provenance",
                                    f"portal id={r['id']} provider={r['provider']} is VERIFIED/ACTIVE with zero provenance records"))


def _check_invalid_urls(conn, report: DoctorReport) -> None:
    rows = conn.execute("SELECT id, careers_url, canonical_url FROM registry_portals").fetchall()
    for r in rows:
        if r["careers_url"] and not is_valid_http_url(r["careers_url"]):
            report.issues.append(Issue("warning", "invalid_careers_url", f"portal id={r['id']} careers_url='{r['careers_url']}'"))
        if r["canonical_url"] and not is_valid_http_url(r["canonical_url"]):
            report.issues.append(Issue("warning", "invalid_canonical_url", f"portal id={r['id']} canonical_url='{r['canonical_url']}'"))


def _check_unsupported_marked_active(conn, report: DoctorReport) -> None:
    rows = conn.execute(
        "SELECT id, provider, support_level FROM registry_portals WHERE verification_status IN ('ACTIVE','VERIFIED') AND support_level = ?",
        (SupportLevel.UNSUPPORTED.value,),
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "unsupported_marked_active",
                                    f"portal id={r['id']} provider={r['provider']} is ACTIVE/VERIFIED but support_level=UNSUPPORTED"))


def _check_orphan_provenance(conn, report: DoctorReport) -> None:
    rows = conn.execute(
        """SELECT p.id FROM registry_provenance p
           LEFT JOIN registry_portals rp ON rp.id = p.portal_id
           WHERE p.portal_id IS NOT NULL AND rp.id IS NULL"""
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("warning", "orphan_provenance", f"provenance id={r['id']} references a missing portal_id"))


def _check_contradictory_domain_mappings(conn, report: DoctorReport) -> None:
    rows = conn.execute(
        """SELECT primary_domain, COUNT(DISTINCT normalized_name) AS n
           FROM registry_companies WHERE primary_domain != '' GROUP BY primary_domain HAVING n > 1"""
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("warning", "contradictory_domain_mapping",
                                    f"domain '{r['primary_domain']}' is mapped to {r['n']} differently-named companies"))


def _check_impossible_scheduler_state(conn, report: DoctorReport) -> None:
    rows = conn.execute(
        "SELECT id, verification_status FROM registry_portals WHERE verification_status IN ('STALE','QUARANTINED','DISABLED') AND next_poll_at IS NOT NULL"
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("warning", "impossible_scheduler_state",
                                    f"portal id={r['id']} status={r['verification_status']} still has a next_poll_at set"))
