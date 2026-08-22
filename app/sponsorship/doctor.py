"""Sponsorship intelligence integrity checker ("sponsorship doctor"),
CLAUDE.md Phase 7 section 35. Read-only: reports problems, never silently
repairs them. `python -m app.sponsorship.cli doctor` exits nonzero when any
SERIOUS issue is found."""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.db import db_session
from app.sponsorship import aliases as aliases_mod
from app.sponsorship import relationships as relationships_mod


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
        _check_orphan_evidence(conn, report)
        _check_invalid_fiscal_years(conn, report)
        _check_alias_collisions(report)
        _check_relationship_contradictions(report)
        _check_confirmed_decision_missing_current_evidence(conn, report)
        _check_no_sponsorship_contradicted_by_state(conn, report)
        _check_pending_identity_review_backlog(conn, report)
    return report


def _check_orphan_evidence(conn, report: DoctorReport) -> None:
    rows = conn.execute(
        """SELECT e.id FROM employer_sponsorship_evidence e
           LEFT JOIN registry_companies c ON c.id = e.company_id
           WHERE e.company_id IS NOT NULL AND c.id IS NULL"""
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "orphan_evidence_company_id",
                                    f"evidence id={r['id']} references a missing registry company"))


def _check_invalid_fiscal_years(conn, report: DoctorReport) -> None:
    current_year = datetime.now(timezone.utc).year
    rows = conn.execute(
        "SELECT id, fiscal_year FROM employer_sponsorship_evidence WHERE fiscal_year IS NOT NULL "
        "AND (fiscal_year < 1990 OR fiscal_year > ?)",
        (current_year + 1,),
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "invalid_fiscal_year",
                                    f"evidence id={r['id']} has implausible fiscal_year={r['fiscal_year']}"))


def _check_alias_collisions(report: DoctorReport) -> None:
    for collision in aliases_mod.list_alias_collisions():
        report.issues.append(Issue("serious", "verified_alias_collision",
                                    f"alias '{collision['normalized_alias']}' is verified for {collision['n']} different companies"))


def _check_relationship_contradictions(report: DoctorReport) -> None:
    for c in relationships_mod.find_contradictions():
        report.issues.append(Issue("serious", "parent_subsidiary_contradiction",
                                    f"company {c['parent_company_id']} and {c['child_company_id']} are each recorded as the other's PARENT"))


def _check_confirmed_decision_missing_current_evidence(conn, report: DoctorReport) -> None:
    rows = conn.execute(
        "SELECT id, job_id, current_job_evidence FROM sponsorship_decisions WHERE status = 'CONFIRMED_SPONSOR'"
    ).fetchall()
    for r in rows:
        try:
            evidence = json.loads(r["current_job_evidence"] or "[]")
        except (ValueError, TypeError):
            evidence = []
        if not evidence:
            report.issues.append(Issue("serious", "confirmed_without_current_evidence",
                                        f"decision id={r['id']} job_id={r['job_id']} is CONFIRMED_SPONSOR with no recorded current-role evidence"))


def _check_no_sponsorship_contradicted_by_state(conn, report: DoctorReport) -> None:
    """A job whose CURRENT sponsorship_status is NO_SPONSORSHIP must never be
    sitting in an apply-eligible application_state -- this is the concrete,
    checkable invariant behind 'history never overrides NO_SPONSORSHIP'."""
    rows = conn.execute(
        "SELECT id, application_state FROM jobs WHERE sponsorship_status = 'NO_SPONSORSHIP' "
        "AND application_state IN ('READY_TO_APPLY', 'APPLIED', 'INTERVIEW')"
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "no_sponsorship_not_hard_skipped",
                                    f"job id={r['id']} is NO_SPONSORSHIP but application_state={r['application_state']}"))


def _check_pending_identity_review_backlog(conn, report: DoctorReport) -> None:
    row = conn.execute("SELECT COUNT(*) AS c FROM employer_identity_review WHERE status = 'PENDING'").fetchone()
    if row["c"] > 0:
        report.issues.append(Issue("warning", "pending_identity_review_backlog",
                                    f"{row['c']} employer identity review item(s) awaiting resolution"))
