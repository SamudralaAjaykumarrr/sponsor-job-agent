"""Application executor integrity checker ("application doctor" -- CLAUDE.md
Phase 8 section 58). Read-only: reports problems, never silently repairs
them. `python -m app.applications.cli doctor` exits nonzero on any SERIOUS
issue, mirroring app.registry.doctor / app.sponsorship.doctor."""

from dataclasses import dataclass, field

from app.applications.models import ExecutionMode
from app.applications.provider_registry import get_application_provider
from app.db import db_session
from app.jobs_repo import get_job
from app.matching.employment_type import classify_employment_type
from app.models import EmploymentType, SponsorshipStatus


@dataclass
class Issue:
    severity: str
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
            "serious_count": self.serious_count, "warning_count": self.warning_count,
            "issues": [{"severity": i.severity, "check": i.check, "detail": i.detail} for i in self.issues],
        }


def run_doctor() -> DoctorReport:
    report = DoctorReport()
    with db_session() as conn:
        _check_applied_without_confirmation(conn, report)
        _check_execution_missing_job(conn, report)
        _check_duplicate_active_execution(conn, report)
        _check_wrong_resume_job_mapping(conn, report)
        _check_missing_answer_snapshot(conn, report)
        _check_unsupported_provider_auto_submit(conn, report)
        _check_non_full_time_in_submission(conn, report)
        _check_unknown_sponsorship_submitted(conn, report)
        _check_likely_sponsorship_auto_submitted(conn, report)
        _check_submitted_without_permitted_policy(conn, report)
    return report


def _check_applied_without_confirmation(conn, report: DoctorReport) -> None:
    rows = conn.execute(
        "SELECT execution_id, job_id FROM application_executions "
        "WHERE status = 'APPLIED' AND (confirmation_id IS NULL OR confirmation_id = '') "
        "AND (user_action_reason IS NULL OR user_action_reason = '')"
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "applied_without_confirmation",
                                    f"execution {r['execution_id']} (job {r['job_id']}) is APPLIED with no confirmation evidence"))


def _check_execution_missing_job(conn, report: DoctorReport) -> None:
    rows = conn.execute(
        "SELECT e.execution_id, e.job_id FROM application_executions e "
        "LEFT JOIN jobs j ON j.id = e.job_id WHERE j.id IS NULL"
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "execution_missing_job",
                                    f"execution {r['execution_id']} references missing job_id {r['job_id']}"))


def _check_duplicate_active_execution(conn, report: DoctorReport) -> None:
    rows = conn.execute(
        "SELECT job_id, COUNT(*) AS n FROM application_executions WHERE active = 1 GROUP BY job_id HAVING n > 1"
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "duplicate_active_execution",
                                    f"job {r['job_id']} has {r['n']} active executions"))


def _check_wrong_resume_job_mapping(conn, report: DoctorReport) -> None:
    rows = conn.execute(
        "SELECT execution_id, job_id, resume_artifact_path FROM application_executions "
        "WHERE resume_artifact_path IS NOT NULL AND resume_artifact_path != ''"
    ).fetchall()
    for r in rows:
        path = r["resume_artifact_path"]
        expected_suffix = f"/{r['job_id']}/"
        if expected_suffix not in path.replace("\\", "/"):
            report.issues.append(Issue("serious", "wrong_resume_job_mapping",
                                        f"execution {r['execution_id']} (job {r['job_id']}) resume path '{path}' "
                                        f"does not correspond to its job_id"))


def _check_missing_answer_snapshot(conn, report: DoctorReport) -> None:
    rows = conn.execute(
        """SELECT e.execution_id, e.job_id FROM application_executions e
           LEFT JOIN application_answer_snapshots s ON s.execution_id = e.execution_id
           WHERE e.status NOT IN ('QUEUED', 'STARTED') AND s.id IS NULL
           GROUP BY e.execution_id"""
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "missing_answer_snapshot",
                                    f"execution {r['execution_id']} (job {r['job_id']}) has no answer snapshot"))


def _check_unsupported_provider_auto_submit(conn, report: DoctorReport) -> None:
    rows = conn.execute(
        "SELECT execution_id, job_id, provider FROM application_executions "
        "WHERE status IN ('SUBMITTED', 'APPLIED') AND submission_method != ''"
    ).fetchall()
    for r in rows:
        job = get_job(r["job_id"])
        if job is None:
            continue
        cap = get_application_provider(job).get_capabilities()
        if not cap.submission_supported:
            report.issues.append(Issue("serious", "unsupported_provider_auto_submit",
                                        f"execution {r['execution_id']} submitted via provider "
                                        f"'{r['provider']}' whose submission_supported=False"))


def _check_non_full_time_in_submission(conn, report: DoctorReport) -> None:
    rows = conn.execute(
        "SELECT execution_id, job_id FROM application_executions WHERE status IN ('SUBMITTING','SUBMITTED','APPLIED')"
    ).fetchall()
    for r in rows:
        job = get_job(r["job_id"])
        if job is None:
            continue
        etype = classify_employment_type(job.employment_type, job.title, job.description)
        if etype != EmploymentType.FULL_TIME:
            report.issues.append(Issue("serious", "non_full_time_in_submission",
                                        f"execution {r['execution_id']} (job {r['job_id']}) reached submission "
                                        f"with employment_type={etype.value}"))


def _check_unknown_sponsorship_submitted(conn, report: DoctorReport) -> None:
    rows = conn.execute(
        "SELECT execution_id, job_id FROM application_executions WHERE status IN ('SUBMITTED','APPLIED')"
    ).fetchall()
    for r in rows:
        job = get_job(r["job_id"])
        if job is not None and job.sponsorship_status == SponsorshipStatus.UNKNOWN:
            report.issues.append(Issue("serious", "unknown_sponsorship_submitted",
                                        f"execution {r['execution_id']} (job {r['job_id']}) submitted with "
                                        f"sponsorship_status=UNKNOWN"))
        if job is not None and job.sponsorship_status == SponsorshipStatus.NO_SPONSORSHIP:
            report.issues.append(Issue("serious", "no_sponsorship_submitted",
                                        f"execution {r['execution_id']} (job {r['job_id']}) submitted with "
                                        f"sponsorship_status=NO_SPONSORSHIP"))


def _check_likely_sponsorship_auto_submitted(conn, report: DoctorReport) -> None:
    rows = conn.execute(
        "SELECT execution_id, job_id, mode FROM application_executions WHERE status IN ('SUBMITTED','APPLIED')"
    ).fetchall()
    for r in rows:
        job = get_job(r["job_id"])
        if job is not None and job.sponsorship_status == SponsorshipStatus.LIKELY_SPONSOR \
                and r["mode"] == ExecutionMode.AUTO_PERMITTED.value:
            report.issues.append(Issue("serious", "likely_sponsorship_auto_submitted",
                                        f"execution {r['execution_id']} (job {r['job_id']}) LIKELY_SPONSOR job "
                                        f"submitted in AUTO_PERMITTED mode"))


def _check_submitted_without_permitted_policy(conn, report: DoctorReport) -> None:
    rows = conn.execute(
        "SELECT execution_id, job_id, automation_policy, mode FROM application_executions "
        "WHERE status IN ('SUBMITTED','APPLIED') AND mode = ?",
        (ExecutionMode.AUTO_PERMITTED.value,),
    ).fetchall()
    for r in rows:
        if r["automation_policy"] != "PERMITTED_AUTO":
            report.issues.append(Issue("serious", "submitted_without_permitted_policy",
                                        f"execution {r['execution_id']} (job {r['job_id']}) auto-submitted with "
                                        f"automation_policy='{r['automation_policy']}' (expected PERMITTED_AUTO)"))
