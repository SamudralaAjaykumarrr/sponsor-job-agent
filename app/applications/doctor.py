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
        # --- Phase 11 (CLAUDE.md Phase 11 section 52) ---
        _check_paused_session_holding_lease(conn, report)
        _check_browser_session_owner_conflict(conn, report)
        _check_stale_capability_evidence(report)
        _check_invalid_step_progress(conn, report)
        _check_real_provider_capability_auto_without_authorization(report)
        _check_false_confirmation_evidence(conn, report)
        _check_duplicate_detected_execution_marked_applied(conn, report)
        # --- Phase 12 (CLAUDE.md Phase 12 section 69) ---
        _check_unsafe_redirect_allowlist(report)
        _check_stage_transition_invalid(conn, report)
        _check_job_identity_mismatch_unresolved(conn, report)
        _check_workday_universal_claim_from_one_tenant(report)
        # --- Phase 13 (CLAUDE.md Phase 13 section 62) ---
        _check_provider_healthy_from_stale_evidence(report)
        _check_closed_job_queued(conn, report)
        _check_stale_resume_jd_mismatch(conn, report)
        _check_captcha_blocked_session_marked_automated(conn, report)
        _check_checkpoint_inconsistency(conn, report)
        _check_unsafe_retry_state(conn, report)
        _check_identity_mismatch_but_session_active(conn, report)
        _check_applied_with_weak_confirmation(conn, report)
        _check_job_identity_unverified_not_surfaced(conn, report)
        # --- Workday/SmartRecruiters/Workable browser-assist hardening (2026-08-22) ---
        _check_validation_blocked_sessions_surfaced(conn, report)
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


# --- Phase 11 checks (CLAUDE.md Phase 11 section 52) -------------------------

def _check_paused_session_holding_lease(conn, report: DoctorReport) -> None:
    """CLAUDE.md Phase 11 section 27: a session waiting on a user action
    must not keep a distributed lease held forever -- app.applications.
    browser_assist releases the lease at the end of every orchestration
    call regardless of the resulting status, so a PAUSED_* session with a
    still-unexpired lease indicates that release didn't happen (a crash
    mid-call, or a future regression)."""
    now = datetime.now(timezone.utc).isoformat()
    rows = conn.execute(
        "SELECT session_id, job_id, status FROM browser_assist_sessions "
        "WHERE status LIKE 'PAUSED_%' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL "
        "AND lease_expires_at > ?",
        (now,),
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("warning", "paused_session_holding_lease",
                                    f"session {r['session_id']} (job {r['job_id']}) is {r['status']} but still "
                                    f"holds an active lease -- it should have been released so another worker "
                                    f"can resume it"))


def _check_browser_session_owner_conflict(conn, report: DoctorReport) -> None:
    """CLAUDE.md Phase 11 section 26: claim_session() atomically sets
    `lease_owner` and `worker_id` to the SAME value together -- these two
    columns disagreeing while a lease is still unexpired means the
    ownership bookkeeping is corrupted, not that two workers both hold it
    (the schema's partial-unique-index-backed `active=1` guarantee already
    makes true dual ownership of one job's session impossible)."""
    now = datetime.now(timezone.utc).isoformat()
    rows = conn.execute(
        "SELECT session_id, job_id, worker_id, lease_owner FROM browser_assist_sessions "
        "WHERE lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL AND lease_expires_at > ? "
        "AND worker_id != lease_owner",
        (now,),
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "browser_session_owner_conflict",
                                    f"session {r['session_id']} (job {r['job_id']}) has lease_owner="
                                    f"'{r['lease_owner']}' but worker_id='{r['worker_id']}' -- inconsistent "
                                    f"ownership bookkeeping"))


def _check_stale_capability_evidence(report: DoctorReport) -> None:
    """CLAUDE.md Phase 11 section 43: surfaces stale LIVE_PUBLIC evidence
    for revalidation -- never auto-disables the capability, only reports."""
    from app.applications.capability_evidence import list_stale

    for result in list_stale():
        report.issues.append(Issue("warning", "stale_capability_evidence",
                                    f"provider '{result.row['provider']}' capability "
                                    f"'{result.row['capability']}' evidence is {result.age_days:.1f} days old -- "
                                    f"revalidation recommended"))


def _check_invalid_step_progress(conn, report: DoctorReport) -> None:
    """CLAUDE.md Phase 11 section 19: total_steps_if_known must never be
    invented (only ever set alongside EXACT confidence), and current_step
    must never exceed a genuinely known total."""
    rows = conn.execute(
        "SELECT session_id, job_id, current_step, total_steps_if_known FROM browser_assist_sessions "
        "WHERE total_steps_if_known IS NOT NULL AND current_step > total_steps_if_known"
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "invalid_step_progress",
                                    f"session {r['session_id']} (job {r['job_id']}) has current_step="
                                    f"{r['current_step']} exceeding total_steps_if_known="
                                    f"{r['total_steps_if_known']}"))
    rows2 = conn.execute(
        "SELECT session_id, job_id FROM browser_assist_sessions "
        "WHERE total_steps_if_known IS NOT NULL AND step_confidence = 'UNKNOWN'"
    ).fetchall()
    for r in rows2:
        report.issues.append(Issue("serious", "invented_total_steps",
                                    f"session {r['session_id']} (job {r['job_id']}) has total_steps_if_known set "
                                    f"but step_confidence=UNKNOWN -- a total must never be recorded without a "
                                    f"genuinely parsed (EXACT) reading"))


def _check_real_provider_capability_auto_without_authorization(report: DoctorReport) -> None:
    """CLAUDE.md Phase 11 section 3: a static assertion (not just a live-flag
    check like `_check_auto_submit_enabled_for_unvalidated_provider` above)
    that no real ATS provider has ever had submission_supported flipped to
    True -- the mock_ats fixture remains the only one, full stop."""
    for cap in all_application_capabilities():
        if cap["provider"] != "mock_ats" and cap["submission_supported"]:
            report.issues.append(Issue("serious", "real_provider_auto_submit_without_authorization",
                                        f"provider '{cap['provider']}' declares submission_supported=True but is "
                                        f"not the mock_ats fixture -- no real ATS provider may set this without "
                                        f"genuine, tested, explicitly-permitted authorization"))


def _check_false_confirmation_evidence(conn, report: DoctorReport) -> None:
    """CLAUDE.md Phase 11 section 34: a CONFIRMED browser-assist session
    must always carry SOME genuine evidence (a confirmation id or a
    confirmation-text fingerprint) -- never bare status with nothing behind
    it."""
    rows = conn.execute(
        "SELECT session_id, job_id FROM browser_assist_sessions WHERE status = 'CONFIRMED' "
        "AND (confirmation_id IS NULL OR confirmation_id = '') "
        "AND (confirmation_text_fingerprint IS NULL OR confirmation_text_fingerprint = '')"
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "false_confirmation_evidence",
                                    f"session {r['session_id']} (job {r['job_id']}) is CONFIRMED with no "
                                    f"confirmation_id and no confirmation_text_fingerprint on record"))


def _check_duplicate_detected_execution_marked_applied(conn, report: DoctorReport) -> None:
    """CLAUDE.md Phase 11 section 36: 'you already applied' evidence must
    never, by itself, produce a fresh APPLIED transition on the linked
    execution."""
    rows = conn.execute(
        "SELECT s.session_id, s.execution_id FROM browser_assist_sessions s "
        "JOIN application_executions e ON e.execution_id = s.execution_id "
        "WHERE s.status = 'DUPLICATE_APPLICATION_DETECTED' AND e.status = 'APPLIED'"
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "duplicate_detected_execution_marked_applied",
                                    f"session {r['session_id']} is DUPLICATE_APPLICATION_DETECTED but its linked "
                                    f"execution {r['execution_id']} is APPLIED -- duplicate-application evidence "
                                    f"must never produce a fresh APPLIED transition"))


# --- Phase 12 checks (CLAUDE.md Phase 12 section 69) --------------------------

def _check_unsafe_redirect_allowlist(report: DoctorReport) -> None:
    """CLAUDE.md Phase 12 sections 9, 63: a static assertion that
    `app.applications.trusted_redirects` only ever trusts REAL, specific ATS
    vendor domain suffixes (never a bare/near-empty suffix, never a generic
    top-level domain like 'com') -- a broad entry here would silently turn
    the trusted-redirect model into the 'any external link is fine' allowlist
    CLAUDE.md section 9 explicitly forbids."""
    from app.applications.trusted_redirects import _ALL_TRUSTED_SUFFIXES

    for suffix in _ALL_TRUSTED_SUFFIXES:
        normalized = suffix.strip(".").lower()
        if len(normalized) < 4 or "." not in normalized:
            report.issues.append(Issue("serious", "unsafe_redirect_allowlist",
                                        f"trusted-redirect suffix '{suffix}' is too broad/malformed to be a real "
                                        f"ATS vendor domain -- the trusted-redirect model must never trust a "
                                        f"generic or near-empty suffix"))


def _check_stage_transition_invalid(conn, report: DoctorReport) -> None:
    """CLAUDE.md Phase 12 sections 28-29, 69: surfaces every logged
    genuinely-anomalous stage regression (see
    app.applications.apply_entry.is_valid_stage_transition) -- never
    blocking, only ever reported for review, matching this project's
    existing 'doctor reports, never repairs' contract."""
    rows = conn.execute(
        "SELECT session_id, detail, created_at FROM browser_spa_events WHERE event = 'stage_transition_invalid' "
        "ORDER BY id DESC LIMIT 100"
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("warning", "stage_transition_invalid",
                                    f"session {r['session_id']} recorded an unexpected stage transition "
                                    f"({r['detail']}) at {r['created_at']}"))


def _check_job_identity_mismatch_unresolved(conn, report: DoctorReport) -> None:
    """CLAUDE.md Phase 12 section 38-39: a session paused for a job-identity
    mismatch must always be surfaced with needs_user_action set -- a
    PAUSED_JOB_IDENTITY_MISMATCH row with needs_user_action=0 would mean the
    safety pause never actually reached the user."""
    rows = conn.execute(
        "SELECT session_id, job_id FROM browser_assist_sessions "
        "WHERE status = 'PAUSED_JOB_IDENTITY_MISMATCH' AND needs_user_action = 0"
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "job_identity_mismatch_not_surfaced",
                                    f"session {r['session_id']} (job {r['job_id']}) is "
                                    f"PAUSED_JOB_IDENTITY_MISMATCH but needs_user_action=0 -- the mismatch pause "
                                    f"was not actually surfaced for review"))


def _check_workday_universal_claim_from_one_tenant(report: DoctorReport) -> None:
    """CLAUDE.md Phase 12 sections 20-21, 68, 77: the hand-curated browser
    capability matrix's Workday row must never claim LIVE_FORM_VERIFIED
    unless AT LEAST ONE tenant/site has genuinely repeated, STABLE evidence
    (app.applications.workday_tenant.classify_stability) -- a single
    attempt, or only VARIABLE/UNVERIFIED tenants, must never be generalized
    into a blanket 'Workday supported' claim."""
    from app.applications.browser_capability_matrix import BrowserVerification, all_rows
    from app.applications.workday_tenant import WorkdayStability, stability_report

    workday_row = next((r for r in all_rows() if r["provider"] == "workday"), None)
    if workday_row is None or workday_row["verification"] != BrowserVerification.LIVE_FORM_VERIFIED.value:
        return
    stable_tenants = [s for s in stability_report() if s.stability == WorkdayStability.STABLE]
    if not stable_tenants:
        report.issues.append(Issue("serious", "workday_universal_claim_from_one_tenant",
                                    "browser_capability_matrix claims workday=LIVE_FORM_VERIFIED but no tenant/site "
                                    "has genuinely repeated STABLE evidence in workday_tenant_attempts -- never "
                                    "generalize from a single observation"))


# =============================================================================
# Phase 13 (CLAUDE.md Phase 13 section 62).
# =============================================================================

def _check_provider_healthy_from_stale_evidence(report: DoctorReport) -> None:
    """CLAUDE.md Phase 13 sections 15, 62: a provider that was PREVIOUSLY
    genuinely form-verified (row.form_verified=1, i.e. it once passed
    real-browser discovery cleanly) but whose evidence has since gone STALE
    must be surfaced for revalidation -- application assist should require
    review until revalidated, never continue to be silently trusted just
    because it worked once. app.applications.provider_health.compute_health
    already computes STALE live on every read (never cached), so this check
    exists to make that fact actionable in the doctor report rather than
    only visible on the dashboard."""
    from app.applications.provider_health import ProviderAssistHealth, list_health

    for entry in list_health():
        row = entry["row"]
        if row.get("form_verified") and entry["health"] == ProviderAssistHealth.STALE.value:
            report.issues.append(Issue(
                "warning", "provider_healthy_from_stale_evidence",
                f"provider {row['provider']} (tenant={row['tenant']}, site={row['site']}) was previously "
                f"form-verified but its evidence is now STALE -- requires revalidation before further trust",
            ))


def _check_closed_job_queued(conn, report: DoctorReport) -> None:
    """CLAUDE.md Phase 13 section 42: a job already marked JOB_NO_LONGER_ACTIVE
    must never still have an active execution or browser-assist session
    queued/in-progress -- preparation must stop the moment a job closes."""
    rows = conn.execute(
        "SELECT e.execution_id, e.job_id FROM application_executions e "
        "JOIN jobs j ON j.id = e.job_id "
        "WHERE e.active = 1 AND j.application_state = 'JOB_NO_LONGER_ACTIVE'"
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "closed_job_queued",
                                    f"execution {r['execution_id']} (job {r['job_id']}) is still active but the job "
                                    f"is JOB_NO_LONGER_ACTIVE"))


def _check_stale_resume_jd_mismatch(conn, report: DoctorReport) -> None:
    """CLAUDE.md Phase 13 sections 43-45: a job with an active execution or
    browser-assist session whose resume was generated against a JD
    fingerprint different from the job's CURRENT one must be flagged -- the
    resume should have been regenerated before further preparation/upload."""
    rows = conn.execute(
        """SELECT j.id AS job_id, j.resume_jd_fingerprint, j.jd_sponsorship_fingerprint
           FROM jobs j
           WHERE j.resume_jd_fingerprint IS NOT NULL AND j.resume_jd_fingerprint != ''
             AND j.jd_sponsorship_fingerprint IS NOT NULL AND j.jd_sponsorship_fingerprint != ''
             AND j.resume_jd_fingerprint != j.jd_sponsorship_fingerprint
             AND EXISTS (SELECT 1 FROM application_executions e WHERE e.job_id = j.id AND e.active = 1)"""
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("warning", "stale_resume_jd_mismatch",
                                    f"job {r['job_id']} has an active execution but its resume was generated "
                                    f"against a different JD fingerprint than the job's current one -- regenerate "
                                    f"before uploading"))


def _check_captcha_blocked_session_marked_automated(conn, report: DoctorReport) -> None:
    """A session currently paused on a CAPTCHA must never belong to an
    execution whose mode is AUTO_PERMITTED -- CAPTCHA presence always means
    ASSIST/manual handoff, never unattended automation."""
    rows = conn.execute(
        """SELECT s.session_id, s.job_id, e.execution_id, e.mode
           FROM browser_assist_sessions s
           JOIN application_executions e ON e.execution_id = s.execution_id
           WHERE s.status = 'PAUSED_CAPTCHA' AND e.mode = 'AUTO_PERMITTED'"""
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "captcha_blocked_session_marked_automated",
                                    f"session {r['session_id']} (job {r['job_id']}) is PAUSED_CAPTCHA but its "
                                    f"linked execution {r['execution_id']} mode is AUTO_PERMITTED"))


def _check_checkpoint_inconsistency(conn, report: DoctorReport) -> None:
    """CLAUDE.md Phase 13 sections 37-38, 62: flags a session whose recorded
    checkpoint history regressed to an earlier reversible stage with no
    reconstruction recorded in between (app.applications.checkpoints.
    find_ordering_anomalies) -- advisory, never blocking."""
    from app.applications.checkpoints import find_ordering_anomalies

    session_ids = [r["session_id"] for r in conn.execute(
        "SELECT DISTINCT session_id FROM application_checkpoints"
    ).fetchall()]
    for session_id in session_ids:
        for anomaly in find_ordering_anomalies(session_id):
            report.issues.append(Issue("warning", "checkpoint_inconsistency",
                                        f"session {session_id}: {anomaly.reason}"))


def _check_unsafe_retry_state(conn, report: DoctorReport) -> None:
    """CLAUDE.md Phase 13 section 34-35: an execution must never accumulate
    retry attempts on a status this project treats as DO_NOT_RETRY/PERMANENT
    -- PERMANENT_SUBMISSION_FAILURE and DUPLICATE_APPLICATION_BLOCKED are
    terminal-and-final; a subsequent 'submit_attempted' audit event for the
    SAME execution_id after either would mean a blind retry slipped past the
    intended one-attempt-per-row design (CLAUDE.md Phase 8 section 37)."""
    rows = conn.execute(
        """SELECT e.execution_id, e.job_id, e.status, COUNT(a.id) AS retry_events
           FROM application_executions e
           JOIN application_audit_log a ON a.execution_id = e.execution_id
           WHERE e.status IN ('PERMANENT_SUBMISSION_FAILURE', 'DUPLICATE_APPLICATION_BLOCKED')
             AND a.event_type = 'submit_attempted'
           GROUP BY e.execution_id, e.job_id, e.status
           HAVING COUNT(a.id) > 1"""
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "unsafe_retry_state",
                                    f"execution {r['execution_id']} (job {r['job_id']}) is {r['status']} but "
                                    f"recorded {r['retry_events']} submit_attempted events -- a terminal/permanent "
                                    f"status must never be blindly retried"))


def _check_identity_mismatch_but_session_active(conn, report: DoctorReport) -> None:
    """CLAUDE.md Phase 13 acceptance correction: a job_identity_verifications
    row recording anything OTHER than VERIFIED (MISMATCH, or the weaker
    PROBABLE/AMBIGUOUS/INSUFFICIENT that also must not continue unattended)
    must never coexist with a still-active (non-terminal) browser session
    for the same job that isn't itself paused for review -- the check must
    have stopped the flow, regardless of which non-VERIFIED verdict it was."""
    rows = conn.execute(
        """SELECT DISTINCT v.job_id, s.session_id, s.status, v.result
           FROM job_identity_verifications v
           JOIN browser_assist_sessions s ON s.job_id = v.job_id AND s.active = 1
           WHERE v.result != 'VERIFIED' AND s.status NOT LIKE 'PAUSED_%'
             AND s.status NOT IN ('CLOSED', 'EXPIRED', 'CONFIRMED')"""
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "identity_mismatch_but_session_active",
                                    f"job {r['job_id']}: a {r['result']} identity verification was recorded but "
                                    f"session {r['session_id']} is {r['status']}, not paused for review"))


def _check_job_identity_unverified_not_surfaced(conn, report: DoctorReport) -> None:
    """CLAUDE.md Phase 13 acceptance correction: mirrors
    _check_job_identity_mismatch_unresolved for the new
    PAUSED_JOB_IDENTITY_UNVERIFIED status -- a session paused because
    identity could not be confidently verified must always be surfaced with
    needs_user_action set, never silently left unattended-looking."""
    rows = conn.execute(
        "SELECT session_id, job_id FROM browser_assist_sessions "
        "WHERE status = 'PAUSED_JOB_IDENTITY_UNVERIFIED' AND needs_user_action = 0"
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "job_identity_unverified_not_surfaced",
                                    f"session {r['session_id']} (job {r['job_id']}) is "
                                    f"PAUSED_JOB_IDENTITY_UNVERIFIED but needs_user_action=0 -- the pause was not "
                                    f"actually surfaced for review"))


def _check_applied_with_weak_confirmation(conn, report: DoctorReport) -> None:
    """CLAUDE.md Phase 13 sections 49-51: a CONFIRMED browser-assist session
    must carry STRONG or MODERATE confirmation evidence, never WEAK/NONE/
    unset -- mirrors _check_applied_without_confirmation's stricter,
    evidence-STRENGTH-aware successor for the browser-assist path."""
    rows = conn.execute(
        """SELECT session_id, job_id, confirmation_evidence_strength FROM browser_assist_sessions
           WHERE status = 'CONFIRMED'
             AND (confirmation_evidence_strength IS NULL OR confirmation_evidence_strength IN ('', 'WEAK', 'NONE'))"""
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "applied_with_weak_confirmation",
                                    f"session {r['session_id']} (job {r['job_id']}) is CONFIRMED with confirmation "
                                    f"evidence strength '{r['confirmation_evidence_strength'] or 'unset'}' -- only "
                                    f"STRONG/MODERATE evidence may confirm"))


def _check_validation_blocked_sessions_surfaced(conn, report: DoctorReport) -> None:
    """Workday/SmartRecruiters/Workable browser-assist hardening
    (2026-08-22): surfaces every recorded VALIDATION_BLOCKED event (a
    Next/Continue click that neither changed the route nor the field set,
    with real validation-error evidence found on the page -- see
    app.applications.dynamic_validation and
    browser_runtime._do_advance_step) for review. Never blocking, matching
    this project's existing 'doctor reports, never repairs' contract and
    _check_stage_transition_invalid's own pattern for this same
    browser_spa_events table."""
    rows = conn.execute(
        "SELECT session_id, detail, created_at FROM browser_spa_events WHERE event = 'validation_blocked' "
        "ORDER BY id DESC LIMIT 100"
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("warning", "validation_blocked",
                                    f"session {r['session_id']} could not advance past a step -- inline "
                                    f"validation appears to be blocking it ({r['detail']}) at {r['created_at']}"))
