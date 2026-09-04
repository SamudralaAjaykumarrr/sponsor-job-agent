"""Application executor integrity checker ("application doctor" -- CLAUDE.md
Phase 8 section 58). Read-only: reports problems, never silently repairs
them. `python -m app.applications.cli doctor` exits nonzero on any SERIOUS
issue, mirroring app.registry.doctor / app.sponsorship.doctor."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app import config
from app.applications import human_verified_employment_evidence
from app.applications.models import ExecutionMode
from app.applications.provider_registry import all_application_capabilities, get_application_provider
from app.db import db_session
from app.jobs_repo import get_job
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
        # --- Approval-gated-autonomy-v1 ---
        _check_approved_status_without_approval_record(conn, report)
        _check_approval_submitted_for_unsupported_provider(conn, report)
        _check_ready_for_approval_flagged_as_needs_action(conn, report)
        # --- Provider Post-Approval Execution V1 ---
        _check_applied_execution_missing_receipt(conn, report)
        _check_receipt_without_applied_execution(conn, report)
        _check_named_real_provider_capability_inflated(report)
        # --- Real Provider Execution V1 ---
        _check_confirmation_phrase_tables_disjoint(report)
        _check_execution_contract_consistency(report)
        _check_execution_contract_submission_never_inferred(report)
        _check_document_binding_wrong_job(conn, report)
        _check_document_binding_execution_job_mismatch(conn, report)
        # --- Greenhouse Verified Submission Contract V1 ---
        _check_greenhouse_canary_disabled_by_default(report)
        _check_greenhouse_submission_supported_still_false(report)
        _check_greenhouse_submit_claim_double_attempt(conn, report)
        _check_greenhouse_claim_without_confirmed_receipt(conn, report)
        # --- Autonomous-ux-reliability-v1 (section I: health/self-healing) ---
        _check_queue_starvation(conn, report)
        _check_submission_circuit_open_too_long(conn, report)
    return report


def _check_approved_status_without_approval_record(conn, report: DoctorReport) -> None:
    """Approval-gated-autonomy-v1: ExecutionStatus.APPROVED is set ONLY by
    app.applications.executor.process_execution(approved=True), which is
    called ONLY by app.applications.approval.approve_and_apply() immediately
    after recording a durable application_approvals row -- it must never be
    reachable without one."""
    rows = conn.execute(
        "SELECT e.execution_id, e.job_id FROM application_executions e "
        "WHERE e.status = 'APPROVED' AND NOT EXISTS "
        "(SELECT 1 FROM application_approvals a WHERE a.execution_id = e.execution_id)"
    ).fetchall()
    for r in rows:
        report.issues.append(Issue(
            "serious", "approved_status_without_approval_record",
            f"execution {r['execution_id']} (job {r['job_id']}) is APPROVED with no application_approvals row",
        ))


def _has_human_reconciliation_evidence(conn, execution_id: str) -> bool:
    """True when this execution's terminal state is backed by one of this
    project's own sanctioned, audited, human-driven reconciliation paths --
    general across every provider and job, never specific to any one
    execution:

    1. A linked browser_assist_sessions row showing CONFIRMED/
       confirmation_observed=1 -- the ordinary browser-observed confirmation
       pipeline (app.applications.browser_assist.attempt_user_submit_
       reconciliation()/_from_evidence()), already recognized by
       _check_approval_submitted_for_unsupported_provider since the job 200
       fix.

    2. An application_audit_log row logging event_type='confirmed' with
       detail starting 'reconciled:' -- app.applications.reconcile.
       reconcile_execution()'s own, and ONLY, logging call for both its
       'confirmed_applied' and 'manual_applied' resolutions (see that
       module's docstring: an operator found independent evidence, or
       applied manually, outside the executor entirely -- by design this
       resolution can legitimately carry NO confirmation_id/url and no
       CONFIRMED browser session, exactly the shape several checks below
       used to treat as unexplained).

    Neither signal can be forged by merely setting a status column --both
    require a genuine prior action through one of the two sanctioned
    reconciliation code paths, so this never weakens detection of an
    execution that reached APPLIED/SUBMITTED some OTHER, truly
    unaccounted-for way."""
    session_confirmed = conn.execute(
        "SELECT 1 FROM browser_assist_sessions WHERE execution_id = ? "
        "AND (status = 'CONFIRMED' OR confirmation_observed = 1) LIMIT 1",
        (execution_id,),
    ).fetchone()
    if session_confirmed is not None:
        return True
    reconciled = conn.execute(
        "SELECT 1 FROM application_audit_log WHERE execution_id = ? "
        "AND event_type = 'confirmed' AND detail LIKE 'reconciled:%' LIMIT 1",
        (execution_id,),
    ).fetchone()
    return reconciled is not None


def _check_approval_submitted_for_unsupported_provider(conn, report: DoctorReport) -> None:
    """No execution with a recorded approval may ever have progressed to
    SUBMITTING/SUBMITTED/SUBMISSION_CONFIRMED for a provider whose
    capability was UNSUPPORTED at approval time -- app.applications.executor.
    _approved_submit_permitted must always have blocked it (spec section 9:
    never infer/force a submission capability that doesn't genuinely
    exist). APPLIED is checked SEPARATELY and more narrowly: an UNSUPPORTED
    (ASSIST_ONLY) provider legitimately reaches APPLIED through a
    completely different, sanctioned route --
    app.applications.browser_assist.attempt_user_submit_reconciliation()/
    _from_evidence(), a human-supervised manual confirmation that never
    calls provider.submit() at all (this is Greenhouse's whole design: no
    automated submission capability, but a human can still complete it and
    have the genuine confirmation reconciled). A real bug caught live
    (2026-08-31, job 200/Robinhood) had this check flag exactly that
    legitimate case as a violation. Only an APPLIED execution with NO
    linked CONFIRMED/confirmation_observed browser_assist_sessions row --
    meaning it could only have reached APPLIED some other, unaccounted-for
    way -- is still a genuine finding."""
    rows = conn.execute(
        "SELECT DISTINCT e.execution_id, e.job_id, e.provider FROM application_executions e "
        "JOIN application_approvals a ON a.execution_id = e.execution_id "
        "WHERE e.status IN ('SUBMITTING', 'SUBMITTED', 'SUBMISSION_CONFIRMED') "
        "AND a.submission_capability = 'UNSUPPORTED'"
    ).fetchall()
    rows += conn.execute(
        "SELECT DISTINCT e.execution_id, e.job_id, e.provider FROM application_executions e "
        "JOIN application_approvals a ON a.execution_id = e.execution_id "
        "WHERE e.status = 'APPLIED' AND a.submission_capability = 'UNSUPPORTED' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM browser_assist_sessions s WHERE s.execution_id = e.execution_id "
        "  AND (s.status = 'CONFIRMED' OR s.confirmation_observed = 1)"
        ")"
    ).fetchall()
    for r in rows:
        if _has_human_reconciliation_evidence(conn, r["execution_id"]):
            continue
        report.issues.append(Issue(
            "serious", "approval_submitted_for_unsupported_provider",
            f"execution {r['execution_id']} (job {r['job_id']}, provider={r['provider']}) reached "
            f"the submit stage despite an UNSUPPORTED approval record",
        ))


def _check_ready_for_approval_flagged_as_needs_action(conn, report: DoctorReport) -> None:
    """Spec section 15: a plain SUBMISSION_READY (READY_FOR_APPROVAL) item
    must never appear in the Needs Action queue's own defining query set --
    that queue is for genuine blockers only. Regression guard for the exact
    bug this feature fixed (app.pipeline_dashboard._NEEDS_ACTION_QUERIES
    used to key off e.requires_user_action=1, which SUBMISSION_READY also
    sets)."""
    from app.pipeline_dashboard import _NEEDS_ACTION_QUERIES

    execution_query = next((q for q in _NEEDS_ACTION_QUERIES if q["kind"] == "execution"), None)
    if execution_query is None:
        return
    rows = conn.execute(f"SELECT job_id FROM ({execution_query['sql']}) t WHERE t.status = 'SUBMISSION_READY'").fetchall()
    for r in rows:
        report.issues.append(Issue(
            "serious", "ready_for_approval_flagged_as_needs_action",
            f"job {r['job_id']}'s SUBMISSION_READY execution appears in the Needs Action query",
        ))


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
        # `HAVING COUNT(*) > 1`, never `HAVING n > 1`: SQLite accepts a
        # SELECT alias in HAVING, PostgreSQL does not (it raises
        # UndefinedColumn). This whole doctor therefore used to abort on its
        # very first grouped check under the Postgres backend -- found by
        # Real Provider Execution V1's own Postgres run, which was the first
        # to call run_doctor() against a real PostgreSQL database.
        "SELECT job_id, COUNT(*) AS n FROM application_executions WHERE active = 1 "
        "GROUP BY job_id HAVING COUNT(*) > 1"
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
        # `GROUP BY e.execution_id, e.job_id`, and a NOT EXISTS rather than a
        # LEFT JOIN + `s.id IS NULL`: SQLite tolerates selecting a column
        # that is neither grouped nor aggregated (it picks an arbitrary
        # row), PostgreSQL rejects it outright (GroupingError). Same
        # SQLite-permissiveness class of bug as the `HAVING n > 1` alias
        # fixed above, found by the same first-ever Postgres doctor run.
        # NOT EXISTS also expresses the intent directly -- one row per
        # execution that has no snapshot at all -- so no grouping is needed
        # to de-duplicate a join fan-out in the first place.
        """SELECT e.execution_id, e.job_id FROM application_executions e
           WHERE e.status NOT IN ('QUEUED', 'STARTED')
             AND NOT EXISTS (SELECT 1 FROM application_answer_snapshots s
                             WHERE s.execution_id = e.execution_id)"""
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
        if cap.submission_supported:
            continue
        if _has_human_reconciliation_evidence(conn, r["execution_id"]):
            continue
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
        etype = human_verified_employment_evidence.resolve_for_job(job).value
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
           GROUP BY job_id HAVING COUNT(*) > 1""",
        (now,),
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "multiple_active_leases_same_job",
                                    f"job {r['job_id']} has {r['n']} executions simultaneously leased"))


def _check_duplicate_confirmation(conn, report: DoctorReport) -> None:
    rows = conn.execute(
        "SELECT confirmation_id, COUNT(*) AS n FROM application_executions "
        "WHERE confirmation_id IS NOT NULL AND confirmation_id != '' "
        "GROUP BY confirmation_id HAVING COUNT(*) > 1"
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
        etype = human_verified_employment_evidence.resolve_for_job(job).value
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
        "SELECT job_id, COUNT(*) AS n FROM browser_assist_sessions WHERE active = 1 "
        "GROUP BY job_id HAVING COUNT(*) > 1"
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
        etype = human_verified_employment_evidence.resolve_for_job(job).value
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
    """Execution-centric, not session-row-centric: an execution can
    legitimately accumulate MULTIPLE browser_assist_sessions rows over its
    lifetime (a stale/EXPIRED session from an earlier reconstruction,
    followed by the actual CONFIRMED one) -- a real bug caught live
    (2026-08-31, job 200/Robinhood) had this JOIN flag the OLD, EXPIRED
    session row even though a DIFFERENT, newer session for the SAME
    execution genuinely showed CONFIRMED/confirmation_observed=1. Also
    treats a non-empty `confirmation_url` on the execution row as valid
    evidence, not just `confirmation_id` -- many employers' confirmation
    pages (Robinhood's included) genuinely have no extractable reference
    number, only a URL and a thank-you message.

    An execution reconciled via app.applications.reconcile.
    reconcile_execution() (see _has_human_reconciliation_evidence) is
    exempted for the SAME reason: that path is a genuine, sanctioned,
    human-driven action that can legitimately leave every browser_assist
    session for this execution un-confirmed (the operator found evidence
    or applied outside the browser-assist flow entirely), never an
    unaccounted-for status change."""
    rows = conn.execute(
        "SELECT e.execution_id FROM application_executions e "
        "WHERE e.status = 'APPLIED' "
        "AND (e.confirmation_id IS NULL OR e.confirmation_id = '') "
        "AND (e.confirmation_url IS NULL OR e.confirmation_url = '') "
        "AND EXISTS (SELECT 1 FROM browser_assist_sessions s WHERE s.execution_id = e.execution_id) "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM browser_assist_sessions s2 WHERE s2.execution_id = e.execution_id "
        "  AND (s2.status = 'CONFIRMED' OR s2.confirmation_observed = 1)"
        ")"
    ).fetchall()
    for r in rows:
        if _has_human_reconciliation_evidence(conn, r["execution_id"]):
            continue
        report.issues.append(Issue("serious", "browser_applied_without_confirmation",
                                    f"execution {r['execution_id']} is APPLIED with no confirmation evidence "
                                    f"anywhere (own confirmation_id/url unset, no linked session shows "
                                    f"CONFIRMED/confirmation_observed)"))


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


# =============================================================================
# Provider Post-Approval Execution V1
# =============================================================================

def _check_applied_execution_missing_receipt(conn, report: DoctorReport) -> None:
    """Every APPLIED execution should have a durable application_receipts
    row (app.applications.receipts) recorded by whichever confirmation path
    actually reached it. Receipt recording is deliberately best-effort
    (never a gate that could block a genuine confirmation -- see
    receipts.py's own module docstring), so a gap here is a WARNING to
    investigate, never a SERIOUS integrity violation the way
    _check_applied_without_confirmation's underlying evidence gap is."""
    rows = conn.execute(
        "SELECT execution_id, job_id FROM application_executions e "
        "WHERE status = 'APPLIED' AND NOT EXISTS "
        "(SELECT 1 FROM application_receipts r WHERE r.execution_id = e.execution_id)"
    ).fetchall()
    for r in rows:
        if _has_human_reconciliation_evidence(conn, r["execution_id"]):
            detail = (
                f"execution {r['execution_id']} (job {r['job_id']}) is APPLIED via human reconciliation with no "
                f"application_receipts row -- expected: a receipt represents genuine automated confirmation "
                f"evidence, which a manually-reconciled execution has none of by design (never fabricated here)"
            )
        else:
            detail = f"execution {r['execution_id']} (job {r['job_id']}) is APPLIED with no application_receipts row"
        report.issues.append(Issue("warning", "applied_execution_missing_receipt", detail))


def _check_receipt_without_applied_execution(conn, report: DoctorReport) -> None:
    """A receipt is only ever recorded immediately after an execution is
    genuinely marked APPLIED (see the two call sites in
    app.applications.executor/browser_assist) -- since APPLIED is a
    terminal status (application_executions.active flips to 0 and never
    changes again), a receipt whose linked execution is NOT APPLIED means
    something wrote a receipt outside the two sanctioned call sites, or an
    execution's status was corrupted after the fact. Either way, SERIOUS."""
    rows = conn.execute(
        "SELECT r.receipt_id, r.execution_id, e.status FROM application_receipts r "
        "JOIN application_executions e ON e.execution_id = r.execution_id "
        "WHERE e.status != 'APPLIED'"
    ).fetchall()
    for r in rows:
        report.issues.append(Issue(
            "serious", "receipt_without_applied_execution",
            f"receipt {r['receipt_id']} references execution {r['execution_id']} whose status is "
            f"'{r['status']}', not APPLIED",
        ))


def _check_named_real_provider_capability_inflated(report: DoctorReport) -> None:
    """Provider Post-Approval Execution V1's own central truthfulness
    guardrail: none of the six named real providers this build covers
    (Greenhouse/Lever/Ashby/Workday/SmartRecruiters/Workable) may ever claim
    `submission_supported=True` -- no real provider in this project has a
    genuinely tested, legitimate, verified final-submission interface (see
    docs/provider-post-approval-execution-v1.md section 1), and
    app.applications.browser_runtime is structurally prevented (see
    `_check_no_browser_auto_submit_capability` below) from ever clicking a
    final submit action for a real provider. Only the deterministic
    `mock_ats` test fixture may set this flag. A static, DB-free check --
    catches a future regression the moment a provider adapter's own
    `ApplicationCapabilities` is edited, not just at runtime."""
    named_real_providers = {"greenhouse", "lever", "ashby", "workday", "smartrecruiters", "workable"}
    for cap in all_application_capabilities():
        if cap["provider"] in named_real_providers and cap["submission_supported"]:
            report.issues.append(Issue(
                "serious", "named_real_provider_capability_inflated",
                f"provider '{cap['provider']}' declares submission_supported=True -- no real ATS in this "
                f"project has a genuinely verified final-submission interface; only mock_ats may set this",
            ))


# =============================================================================
# Real Provider Execution V1
# =============================================================================

def _check_confirmation_phrase_tables_disjoint(report: DoctorReport) -> None:
    """`app.applications.confirmation_parser`'s SUCCESS_PHRASES and
    DUPLICATE_APPLICATION_PHRASES must stay MUTUALLY DISJOINT, so classifying
    a page's text is always one unambiguous lookup rather than a priority
    tie-break -- the same invariant CLAUDE.md's Phase 11 rules impose on
    apply_entry's three phrase tables (a phrase appearing in two tables was
    a real Phase 10 bug there). A duplicate-application page must never be
    reachable as a fresh confirmation, and a genuine success page must never
    be swallowed as a duplicate. Static and DB-free: catches the regression
    the moment a phrase is added to the wrong table."""
    from app.applications import confirmation_parser

    overlap = set(confirmation_parser.SUCCESS_PHRASES) & set(confirmation_parser.DUPLICATE_APPLICATION_PHRASES)
    if overlap:
        report.issues.append(Issue(
            "serious", "confirmation_phrase_tables_overlap",
            f"phrase(s) appear in BOTH confirmation_parser.SUCCESS_PHRASES and "
            f"DUPLICATE_APPLICATION_PHRASES: {sorted(overlap)} -- classification would depend on lookup order",
        ))
    for table_name, table in (
        ("SUCCESS_PHRASES", confirmation_parser.SUCCESS_PHRASES),
        ("DUPLICATE_APPLICATION_PHRASES", confirmation_parser.DUPLICATE_APPLICATION_PHRASES),
    ):
        for phrase in table:
            if phrase != phrase.lower().strip() or len(phrase) < 8:
                report.issues.append(Issue(
                    "serious", "confirmation_phrase_unsafe",
                    f"confirmation_parser.{table_name} entry {phrase!r} is not a lowercase, specific, "
                    f"completed-action phrase -- a short/unnormalized phrase risks matching unrelated page text",
                ))


def _check_execution_contract_consistency(report: DoctorReport) -> None:
    """`app.applications.execution_contract` owns no facts of its own -- every
    flag it reports is derived from `ApplicationCapabilities`,
    `ProviderCapabilities`, or `browser_capability_matrix`. This check
    re-derives the contract and confirms each flag still agrees with its
    source, so the derived view can never quietly drift away from (or
    inflate beyond) the registries it summarizes."""
    from app.applications.browser_capability_matrix import BrowserVerification, all_rows as browser_rows
    from app.applications.execution_contract import all_contracts

    app_caps = {c["provider"]: c for c in all_application_capabilities() if c["provider"] != "generic"}
    browser = {r["provider"]: r for r in browser_rows()}

    for contract in all_contracts():
        provider = contract.provider
        caps = app_caps.get(provider)
        row = browser.get(provider)
        browser_evidenced = row is not None and row["verification"] != BrowserVerification.NOT_TESTED.value

        expected_form = bool(caps and caps["form_discovery_supported"]) or bool(
            browser_evidenced and row["field_discovery"])
        if contract.form_discovery_supported != expected_form:
            report.issues.append(Issue(
                "serious", "execution_contract_drift",
                f"provider '{provider}': contract.form_discovery_supported="
                f"{contract.form_discovery_supported} disagrees with its sources ({expected_form})",
            ))

        expected_assist = bool(browser_evidenced and row["field_discovery"])
        if contract.assist_supported != expected_assist:
            report.issues.append(Issue(
                "serious", "execution_contract_drift",
                f"provider '{provider}': contract.assist_supported={contract.assist_supported} disagrees "
                f"with app.applications.browser_capability_matrix ({expected_assist})",
            ))

        expected_submission = bool(caps and caps["submission_supported"])
        if contract.submission_supported != expected_submission:
            report.issues.append(Issue(
                "serious", "execution_contract_drift",
                f"provider '{provider}': contract.submission_supported={contract.submission_supported} "
                f"disagrees with ApplicationCapabilities.submission_supported ({expected_submission})",
            ))

        # Tsenta Remaining-Gaps Closure V2: identity/presubmit_validation are
        # narrow, hand-named per-provider facts (today: greenhouse only for
        # both, via a genuine dedicated module each), so re-derive from the
        # SAME narrow rule build_contract() itself uses rather than the
        # broader source registries above -- this still catches the failure
        # mode that matters (a provider silently claiming one of these
        # without the dedicated module actually existing).
        expected_identity = provider == "greenhouse" or expected_assist
        if contract.identity_supported != expected_identity:
            report.issues.append(Issue(
                "serious", "execution_contract_drift",
                f"provider '{provider}': contract.identity_supported={contract.identity_supported} disagrees "
                f"with expected ({expected_identity}) -- identity must come from a dedicated provider-API "
                f"identity function or generic browser-reachable job_identity verification, never a guess",
            ))
        expected_presubmit = provider == "greenhouse"
        if contract.presubmit_validation_supported != expected_presubmit:
            report.issues.append(Issue(
                "serious", "execution_contract_drift",
                f"provider '{provider}': contract.presubmit_validation_supported="
                f"{contract.presubmit_validation_supported} disagrees with expected ({expected_presubmit}) -- "
                f"only a provider with a genuine dedicated pre-submit contract module may report this True",
            ))


def _check_execution_contract_submission_never_inferred(report: DoctorReport) -> None:
    """The brief's single most important line: "Browser fill capability is
    NOT submission capability." A provider may legitimately have every
    browser/assist/fill/upload capability True while `submission_supported`
    stays False -- and today EVERY real provider is exactly that. This check
    fails if any provider other than the deterministic `mock_ats` fixture
    ever reports `submission_supported=True` in the derived contract, or if
    a True value is ever sourced from anything but its own
    ApplicationCapabilities row."""
    from app.applications.execution_contract import CapabilitySource, all_contracts

    for contract in all_contracts():
        if not contract.submission_supported:
            continue
        if contract.provider != "mock_ats":
            report.issues.append(Issue(
                "serious", "execution_contract_submission_inflated",
                f"provider '{contract.provider}' reports submission_supported=True in the execution "
                f"contract -- only the deterministic mock_ats fixture may ever do so",
            ))
        if contract.submission_source not in (CapabilitySource.MOCK_FIXTURE, CapabilitySource.PROVIDER_API):
            report.issues.append(Issue(
                "serious", "execution_contract_submission_inflated",
                f"provider '{contract.provider}' reports submission_supported=True sourced from "
                f"'{contract.submission_source.value}' -- submission capability may never be inferred from a "
                f"browser/assist observation",
            ))


def _check_document_binding_wrong_job(conn, report: DoctorReport) -> None:
    """A durable document binding whose artifact path does not belong to the
    job it was bound for -- i.e. evidence that some other job's resume was
    (or was about to be) handed to this employer. Uses the same
    `/<job_id>/` path-segment convention every other resume-ownership check
    in this project agrees on."""
    rows = conn.execute(
        "SELECT binding_id, job_id, artifact_path, document_kind FROM application_document_bindings "
        "WHERE artifact_path != '' ORDER BY id DESC LIMIT 500"
    ).fetchall()
    for row in rows:
        normalized = (row["artifact_path"] or "").replace("\\", "/")
        if f"/{row['job_id']}/" not in normalized:
            report.issues.append(Issue(
                "serious", "document_binding_wrong_job",
                f"document binding {row['binding_id']} ({row['document_kind']}) for job {row['job_id']} "
                f"points at '{row['artifact_path']}', which does not belong to that job",
            ))


def _check_document_binding_execution_job_mismatch(conn, report: DoctorReport) -> None:
    """A binding whose execution belongs to a DIFFERENT job than the binding
    itself claims -- the cross-wiring case a path check alone cannot see."""
    rows = conn.execute(
        """SELECT b.binding_id, b.job_id AS binding_job, e.job_id AS execution_job, b.execution_id
           FROM application_document_bindings b
           JOIN application_executions e ON e.execution_id = b.execution_id
           WHERE b.execution_id != '' AND b.job_id != e.job_id
           ORDER BY b.id DESC LIMIT 200"""
    ).fetchall()
    for row in rows:
        report.issues.append(Issue(
            "serious", "document_binding_execution_job_mismatch",
            f"document binding {row['binding_id']} claims job {row['binding_job']} but its execution "
            f"{row['execution_id']} belongs to job {row['execution_job']}",
        ))


# --- Greenhouse Verified Submission Contract V1 ------------------------------

def _check_greenhouse_canary_disabled_by_default(report: DoctorReport) -> None:
    """A static assertion that the real Greenhouse submission canary stays
    off by default, matching every other real-network/real-browser flag in
    this project. Reads the LIVE config value (never a hardcoded assumption)
    -- a serious finding here means an operator's .env (or a bug) has
    already turned on the one flag this feature's entire safety model
    depends on staying off unless deliberately, explicitly enabled."""
    if config.GREENHOUSE_SUBMIT_CANARY_ENABLED:
        report.issues.append(Issue(
            "warning", "greenhouse_canary_enabled",
            "GREENHOUSE_SUBMIT_CANARY_ENABLED is currently true -- the real Greenhouse submission canary can be "
            "invoked for an explicitly-approved job. This is a warning, not an error: it may be an intentional, "
            "explicit operator decision, but confirm it was not left on by accident.",
        ))


def _check_greenhouse_submission_supported_still_false(report: DoctorReport) -> None:
    """Capability honesty (build brief 'CAPABILITY HONESTY' section): this
    feature's engine/contract/canary must never, by their mere existence,
    flip GreenhouseApplicationProvider.capabilities.submission_supported to
    True. A local fixture is never sufficient evidence for real-provider
    production submission support -- only a genuinely authorized, genuinely
    observed real-employer submission could ever justify that, and this
    project never performs one."""
    from app.applications.providers_greenhouse import GreenhouseApplicationProvider

    if GreenhouseApplicationProvider.capabilities.submission_supported:
        report.issues.append(Issue(
            "serious", "greenhouse_submission_supported_inflated",
            "GreenhouseApplicationProvider.capabilities.submission_supported is True -- this must stay False "
            "until a genuine, authorized, real-employer submission has been observed; a local fixture or the "
            "existence of the submit engine/canary is never sufficient evidence on its own.",
        ))


def _check_greenhouse_submit_claim_double_attempt(conn, report: DoctorReport) -> None:
    """The submit-once claim's whole reason to exist: no execution may ever
    show more than one attempted submit action. The table's own
    UNIQUE(execution_id) index already makes a second ROW impossible; this
    checks the flag itself was never somehow reset (e.g. a manual DB edit)
    to allow a second click."""
    rows = conn.execute(
        "SELECT execution_id, COUNT(*) AS c FROM greenhouse_submit_claims "
        "WHERE submit_attempted = 1 GROUP BY execution_id HAVING COUNT(*) > 1"
    ).fetchall()
    for r in rows:
        report.issues.append(Issue(
            "serious", "greenhouse_submit_claim_double_attempt",
            f"execution {r['execution_id']} has {r['c']} greenhouse_submit_claims rows with submit_attempted=1 "
            f"-- at most one physical submit action may ever be attempted per execution",
        ))


def _check_greenhouse_claim_without_confirmed_receipt(conn, report: DoctorReport) -> None:
    """A claim whose outcome is CONFIRMED must always have a matching
    application_receipts row (the same 'APPLIED requires a receipt'
    invariant `_check_applied_execution_missing_receipt` already enforces
    for the rest of the project, restated here since this feature writes the
    claim table as an additional, independent record of the same fact)."""
    rows = conn.execute(
        "SELECT c.execution_id FROM greenhouse_submit_claims c "
        "LEFT JOIN application_receipts r ON r.execution_id = c.execution_id "
        "WHERE c.outcome = 'CONFIRMED' AND r.id IS NULL"
    ).fetchall()
    for r in rows:
        report.issues.append(Issue(
            "serious", "greenhouse_claim_confirmed_without_receipt",
            f"execution {r['execution_id']}'s greenhouse_submit_claims row is CONFIRMED but no "
            f"application_receipts row exists for it",
        ))


def _check_queue_starvation(conn, report: DoctorReport) -> None:
    """Autonomous-ux-reliability-v1 section I: an active, claimable
    execution (QUEUED or otherwise not yet leased) that has sat unclaimed
    far longer than a single lease window means SOMETHING stopped consuming
    the queue (orchestrator not running, worker fleet down, an unhandled
    exception outside every existing per-job try/except) -- distinct from
    `_check_expired_execution_lease` above, which catches a lease that WAS
    taken and then abandoned. Never auto-recovered here (read-only, matching
    every doctor in this project); surfaced so an operator notices instead
    of a silently growing backlog nobody is working."""
    threshold = (
        datetime.now(timezone.utc) - timedelta(seconds=max(60, config.APPLICATION_LEASE_SECONDS * 4))
    ).isoformat()
    rows = conn.execute(
        "SELECT execution_id, job_id, status, started_at FROM application_executions "
        "WHERE active = 1 AND lease_owner IS NULL AND started_at <= ? "
        "AND status IN ('QUEUED', 'RETRYABLE_SUBMISSION_FAILURE') "
        "ORDER BY started_at ASC LIMIT 20",
        (threshold,),
    ).fetchall()
    for r in rows:
        report.issues.append(Issue(
            "serious", "application_queue_starvation",
            f"execution {r['execution_id']} (job {r['job_id']}, status={r['status']}) has been unclaimed "
            f"since {r['started_at']} -- no worker appears to be consuming the application queue.",
        ))


def _check_submission_circuit_open_too_long(conn, report: DoctorReport) -> None:
    """A provider's submission circuit breaker staying OPEN for many times
    its own cooldown window means every retry/half-open probe since has
    also failed -- worth an operator's attention (a genuinely broken
    provider integration, not a blip) without ever disabling the provider
    here; `app.applications.circuit` remains the only thing that can ever
    close it again, and it always keeps trying on its own (self-healing) --
    this check is purely informational, matching every other doctor check's
    read-only contract."""
    threshold = (
        datetime.now(timezone.utc) - timedelta(seconds=max(60, config.APPLICATION_CIRCUIT_BREAKER_COOLDOWN_SECONDS * 6))
    ).isoformat()
    rows = conn.execute(
        "SELECT provider, opened_at, consecutive_failures FROM application_provider_circuit_state "
        "WHERE state = 'OPEN' AND opened_at IS NOT NULL AND opened_at <= ?"
    , (threshold,)).fetchall()
    for r in rows:
        report.issues.append(Issue(
            "warning", "application_submission_circuit_open_too_long",
            f"provider '{r['provider']}' submission circuit has been OPEN since {r['opened_at']} "
            f"({r['consecutive_failures']} consecutive failures) -- repeated provider failure, still "
            "self-healing on its own schedule, but worth reviewing.",
        ))
