"""Application executor integrity checker ("application doctor" -- CLAUDE.md
Phase 8 section 58). Read-only: reports problems, never silently repairs
them. `python -m app.applications.cli doctor` exits nonzero on any SERIOUS
issue, mirroring app.registry.doctor / app.sponsorship.doctor."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app import config
from app.applications.models import ExecutionMode
from app.applications.provider_registry import all_application_capabilities, get_application_provider
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
        # --- Phase 9 (CLAUDE.md Phase 9 section 48) ---
        _check_expired_execution_lease(conn, report)
        _check_orphan_execution_lease(conn, report)
        _check_multiple_active_leases_same_job(conn, report)
        _check_duplicate_confirmation(conn, report)
        _check_submission_capable_provider_without_policy(report)
        _check_auto_submit_enabled_for_unsupported_provider(report)
        _check_unknown_submission_retried(conn, report)
        _check_non_full_time_queued(conn, report)
        _check_non_confirmed_sponsorship_queued(conn, report)
        _check_rate_limit_accounting_inconsistency(conn, report)
        # --- Phase 10 (CLAUDE.md Phase 10 section 64) ---
        _check_duplicate_active_browser_sessions(conn, report)
        _check_browser_session_without_execution(conn, report)
        _check_browser_session_non_full_time(conn, report)
        _check_browser_session_non_eligible_sponsorship(conn, report)
        _check_stale_browser_session_still_active(conn, report)
        _check_browser_confirmation_without_applied_execution(conn, report)
        _check_browser_applied_without_confirmation(conn, report)
        _check_no_browser_auto_submit_capability(report)
        _check_browser_session_forbidden_fields(conn, report)
        _check_browser_capability_matrix_never_claims_final_submit(report)
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


# --- Phase 9 checks (CLAUDE.md Phase 9 section 48) --------------------------

def _check_expired_execution_lease(conn, report: DoctorReport) -> None:
    now = datetime.now(timezone.utc).isoformat()
    rows = conn.execute(
        "SELECT execution_id, job_id, lease_expires_at FROM application_executions "
        "WHERE active = 1 AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
        (now,),
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("warning", "expired_execution_lease",
                                    f"execution {r['execution_id']} (job {r['job_id']}) has an expired lease "
                                    f"(expired {r['lease_expires_at']}) -- reclaimable, but not yet reclaimed"))


def _check_orphan_execution_lease(conn, report: DoctorReport) -> None:
    """A lease held on an execution that is no longer active=1 is a bug --
    app.applications.queue always releases the lease before/at the point an
    execution reaches a terminal state via the normal worker path."""
    rows = conn.execute(
        "SELECT execution_id, job_id FROM application_executions WHERE active = 0 AND lease_owner IS NOT NULL"
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "orphan_execution_lease",
                                    f"execution {r['execution_id']} (job {r['job_id']}) is terminal but still "
                                    f"holds a lease -- should have been released"))


def _check_multiple_active_leases_same_job(conn, report: DoctorReport) -> None:
    now = datetime.now(timezone.utc).isoformat()
    rows = conn.execute(
        """SELECT job_id, COUNT(*) AS n FROM application_executions
           WHERE lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL AND lease_expires_at > ?
           GROUP BY job_id HAVING n > 1""",
        (now,),
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "multiple_active_leases_same_job",
                                    f"job {r['job_id']} has {r['n']} executions simultaneously leased"))


def _check_duplicate_confirmation(conn, report: DoctorReport) -> None:
    rows = conn.execute(
        "SELECT confirmation_id, COUNT(*) AS n FROM application_executions "
        "WHERE confirmation_id IS NOT NULL AND confirmation_id != '' GROUP BY confirmation_id HAVING n > 1"
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "duplicate_confirmation",
                                    f"confirmation_id '{r['confirmation_id']}' is used by {r['n']} executions"))


def _check_submission_capable_provider_without_policy(report: DoctorReport) -> None:
    for cap in all_application_capabilities():
        if cap["submission_supported"] and cap["automation_policy"] != "PERMITTED_AUTO":
            report.issues.append(Issue("serious", "submission_capable_provider_without_policy",
                                        f"provider '{cap['provider']}' declares submission_supported=True but "
                                        f"automation_policy='{cap['automation_policy']}' (expected PERMITTED_AUTO)"))


def _check_auto_submit_enabled_for_unsupported_provider(report: DoctorReport) -> None:
    if not config.AUTO_SUBMIT_ENABLED:
        return
    for cap in all_application_capabilities():
        if cap["submission_supported"] and not (cap["live_validated"] or cap["provider"] == "mock_ats"):
            report.issues.append(Issue("warning", "auto_submit_enabled_for_unvalidated_provider",
                                        f"AUTO_SUBMIT_ENABLED is true and provider '{cap['provider']}' declares "
                                        f"submission_supported=True but live_validated=False"))


def _check_unknown_submission_retried(conn, report: DoctorReport) -> None:
    rows = conn.execute(
        "SELECT execution_id, job_id, attempt_count FROM application_executions "
        "WHERE status = 'SUBMISSION_STATUS_UNKNOWN' AND attempt_count > 1"
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "unknown_submission_retried",
                                    f"execution {r['execution_id']} (job {r['job_id']}) reached "
                                    f"SUBMISSION_STATUS_UNKNOWN after {r['attempt_count']} submit attempts on the "
                                    f"same execution row -- should never exceed 1"))


def _check_non_full_time_queued(conn, report: DoctorReport) -> None:
    rows = conn.execute("SELECT execution_id, job_id FROM application_executions WHERE active = 1").fetchall()
    for r in rows:
        job = get_job(r["job_id"])
        if job is None:
            continue
        etype = classify_employment_type(job.employment_type, job.title, job.description)
        if etype not in (EmploymentType.FULL_TIME, EmploymentType.UNKNOWN):
            report.issues.append(Issue("serious", "non_full_time_queued",
                                        f"execution {r['execution_id']} (job {r['job_id']}) is active with "
                                        f"employment_type={etype.value}"))


def _check_non_confirmed_sponsorship_queued(conn, report: DoctorReport) -> None:
    rows = conn.execute(
        "SELECT execution_id, job_id FROM application_executions WHERE active = 1 AND mode = ?",
        (ExecutionMode.AUTO_PERMITTED.value,),
    ).fetchall()
    for r in rows:
        job = get_job(r["job_id"])
        if job is not None and job.sponsorship_status != SponsorshipStatus.CONFIRMED_SPONSOR:
            report.issues.append(Issue("serious", "non_confirmed_sponsorship_queued",
                                        f"execution {r['execution_id']} (job {r['job_id']}) is AUTO_PERMITTED with "
                                        f"sponsorship_status={job.sponsorship_status.value}"))


# --- Phase 10 checks (CLAUDE.md Phase 10 section 64) ------------------------

def _check_duplicate_active_browser_sessions(conn, report: DoctorReport) -> None:
    rows = conn.execute(
        "SELECT job_id, COUNT(*) AS n FROM browser_assist_sessions WHERE active = 1 GROUP BY job_id HAVING n > 1"
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "duplicate_active_browser_session",
                                    f"job {r['job_id']} has {r['n']} active browser-assist sessions"))


def _check_browser_session_without_execution(conn, report: DoctorReport) -> None:
    rows = conn.execute(
        "SELECT s.session_id, s.execution_id FROM browser_assist_sessions s "
        "LEFT JOIN application_executions e ON e.execution_id = s.execution_id WHERE e.execution_id IS NULL"
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "browser_session_without_execution",
                                    f"session {r['session_id']} references missing execution {r['execution_id']}"))


def _check_browser_session_non_full_time(conn, report: DoctorReport) -> None:
    rows = conn.execute("SELECT session_id, job_id FROM browser_assist_sessions WHERE active = 1").fetchall()
    for r in rows:
        job = get_job(r["job_id"])
        if job is None:
            continue
        etype = classify_employment_type(job.employment_type, job.title, job.description)
        if etype not in (EmploymentType.FULL_TIME, EmploymentType.UNKNOWN):
            report.issues.append(Issue("serious", "browser_session_non_full_time",
                                        f"session {r['session_id']} (job {r['job_id']}) is active with "
                                        f"employment_type={etype.value}"))


def _check_browser_session_non_eligible_sponsorship(conn, report: DoctorReport) -> None:
    """CLAUDE.md Phase 10 section 2: UNKNOWN/NO_SPONSORSHIP must never even
    enter the browser-assist queue (LIKELY_SPONSOR IS allowed -- review-only,
    never auto-submitted -- so it is deliberately not flagged here)."""
    rows = conn.execute("SELECT session_id, job_id FROM browser_assist_sessions WHERE active = 1").fetchall()
    for r in rows:
        job = get_job(r["job_id"])
        if job is not None and job.sponsorship_status in (SponsorshipStatus.UNKNOWN, SponsorshipStatus.NO_SPONSORSHIP):
            report.issues.append(Issue("serious", "browser_session_non_eligible_sponsorship",
                                        f"session {r['session_id']} (job {r['job_id']}) is active with "
                                        f"sponsorship_status={job.sponsorship_status.value}"))


def _check_stale_browser_session_still_active(conn, report: DoctorReport) -> None:
    cutoff = (datetime.now(timezone.utc)
              - timedelta(minutes=config.BROWSER_SESSION_TIMEOUT_MINUTES * 3)).isoformat()
    rows = conn.execute(
        "SELECT session_id, job_id, last_activity_at FROM browser_assist_sessions "
        "WHERE active = 1 AND last_activity_at < ?", (cutoff,),
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("warning", "stale_browser_session_still_active",
                                    f"session {r['session_id']} (job {r['job_id']}) has had no activity since "
                                    f"{r['last_activity_at']} but is still active -- the stale-session reaper "
                                    f"has not run recently"))


def _check_browser_confirmation_without_applied_execution(conn, report: DoctorReport) -> None:
    rows = conn.execute(
        "SELECT s.session_id, s.execution_id, e.status FROM browser_assist_sessions s "
        "JOIN application_executions e ON e.execution_id = s.execution_id "
        "WHERE s.status = 'CONFIRMED' AND e.status != 'APPLIED'"
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "browser_confirmation_without_applied_execution",
                                    f"session {r['session_id']} is CONFIRMED but linked execution "
                                    f"{r['execution_id']} is {r['status']} (expected APPLIED)"))


def _check_browser_applied_without_confirmation(conn, report: DoctorReport) -> None:
    rows = conn.execute(
        "SELECT s.session_id, s.execution_id FROM browser_assist_sessions s "
        "JOIN application_executions e ON e.execution_id = s.execution_id "
        "WHERE e.status = 'APPLIED' AND s.status != 'CONFIRMED' AND s.confirmation_observed = 0 "
        "AND (e.confirmation_id IS NULL OR e.confirmation_id = '')"
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "browser_applied_without_confirmation",
                                    f"execution linked to browser session {r['session_id']} is APPLIED with "
                                    f"no confirmation evidence anywhere"))


_FORBIDDEN_FIELD_SUBSTRINGS = ("password=", "passwd=", "mfa_code=", "otp=", "secret=", "authorization: bearer",
                               "set-cookie:")


def _check_no_browser_auto_submit_capability(report: DoctorReport) -> None:
    """CLAUDE.md Phase 10 sections 28-29: static assertion that the browser
    runtime never grew a click-the-final-submit-button code path. Checked
    here (not just in a test) so a doctor run in any environment catches a
    regression, not only whichever test file happens to cover it."""
    from app.applications import browser_runtime

    forbidden_name_fragments = ("click_submit", "submit_application", "click_apply", "auto_submit")
    public_names = [n for n in dir(browser_runtime) if not n.startswith("_")]
    for name in public_names:
        lowered = name.lower()
        if any(frag in lowered for frag in forbidden_name_fragments):
            report.issues.append(Issue("serious", "unexpected_browser_auto_submit_capability",
                                        f"app.applications.browser_runtime exposes '{name}' -- browser assist "
                                        f"must never click a final submit/apply action"))


def _check_browser_session_forbidden_fields(conn, report: DoctorReport) -> None:
    """CLAUDE.md Phase 10 section 5: no session row may ever contain a
    password/MFA-code/cookie/token-shaped value. Lightweight substring scan
    over the free-text columns -- a real secret should never appear here at
    all, since no code path in this project ever writes one into these
    columns."""
    rows = conn.execute(
        "SELECT session_id, user_action_reason, confirmation_text_fingerprint FROM browser_assist_sessions"
    ).fetchall()
    for r in rows:
        haystack = f"{r['user_action_reason'] or ''} {r['confirmation_text_fingerprint'] or ''}".lower()
        if any(frag in haystack for frag in _FORBIDDEN_FIELD_SUBSTRINGS):
            report.issues.append(Issue("serious", "browser_session_forbidden_field",
                                        f"session {r['session_id']} appears to contain a forbidden "
                                        f"secret-shaped value"))


def _check_browser_capability_matrix_never_claims_final_submit(report: DoctorReport) -> None:
    """CLAUDE.md Phase 10 sections 28-29, 59: the browser-assist capability
    matrix (app.applications.browser_capability_matrix) must never claim
    final-submit automation for any provider, regardless of verification
    status."""
    from app.applications.browser_capability_matrix import all_rows

    for row in all_rows():
        if row["final_submit_automation"]:
            report.issues.append(Issue("serious", "browser_capability_matrix_claims_final_submit",
                                        f"provider '{row['provider']}' claims final_submit_automation=True in the "
                                        f"browser-assist capability matrix -- must always be False"))


def _check_rate_limit_accounting_inconsistency(conn, report: DoctorReport) -> None:
    hour_cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    hourly = conn.execute(
        "SELECT COUNT(*) AS c FROM application_audit_log WHERE event_type = 'submit_attempted' AND created_at >= ?",
        (hour_cutoff,),
    ).fetchone()["c"]
    if hourly > config.MAX_APPLICATIONS_PER_HOUR:
        report.issues.append(Issue("serious", "rate_limit_accounting_inconsistency",
                                    f"{hourly} submit attempts recorded in the last hour, exceeding "
                                    f"MAX_APPLICATIONS_PER_HOUR={config.MAX_APPLICATIONS_PER_HOUR}"))
