"""Application executor orchestration (CLAUDE.md Phase 8 sections 1-3, 19,
32-37, 45). Two entry points:

  - queue_application(job_id, mode) -- the safe front door. Re-checks
    eligibility/duplicates, creates (or returns an existing) execution row.
    Never itself does any form/network work.
  - process_execution(execution_id) -- does the actual prepare -> map ->
    fill -> validate -> (submit) work for one execution, synchronously.
    Called directly by the CLI/tests, or by a leased worker loop
    (app.applications.queue) for the distributed case.

Nothing in this module ever bypasses APPLICATION_EXECUTOR_ENABLED /
AUTO_SUBMIT_ENABLED, and there is no generic "force submit" parameter
anywhere in this file."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app import config
from app.applications import duplicate, rate_limit, repo
from app.applications.eligibility import evaluate_executor_eligibility
from app.applications.fingerprint import check_and_record_baseline
from app.applications.models import ExecutionMode, ExecutionStatus, PolicyReason
from app.applications.provider_registry import get_application_provider
from app.applications.schema import build_application_fields
from app.candidate.profile import load_profile
from app.jobs_repo import get_job, record_state_change, update_job
from app.models import ApplicationState, Job


class ExecutorDisabledError(Exception):
    """CLAUDE.md Phase 8 sections 63-64: APPLICATION_EXECUTOR_ENABLED is
    False by default. Raised rather than silently no-op-ing so a caller
    (CLI/dashboard) can show an explicit, honest message instead of jobs
    quietly never progressing."""


class AutoSubmitDisabledError(Exception):
    """Raised only when the caller explicitly asked for AUTO_PERMITTED mode
    while AUTO_SUBMIT_ENABLED is False -- ASSIST mode is never affected by
    this flag."""


@dataclass
class QueueResult:
    queued: bool
    execution_id: Optional[str]
    reason: str


def queue_application(job_id: int, mode: str = config.APPLICATION_DEFAULT_MODE) -> QueueResult:
    if not config.APPLICATION_EXECUTOR_ENABLED:
        raise ExecutorDisabledError(
            "APPLICATION_EXECUTOR_ENABLED is false -- set it to true to allow queuing applications."
        )
    mode = ExecutionMode(mode)
    if mode == ExecutionMode.AUTO_PERMITTED and not config.AUTO_SUBMIT_ENABLED:
        raise AutoSubmitDisabledError(
            "AUTO_SUBMIT_ENABLED is false -- AUTO_PERMITTED mode cannot be used until it is enabled."
        )

    job = get_job(job_id)
    if job is None:
        raise ValueError(f"job {job_id} not found")

    existing = repo.get_active_execution_for_job(job_id)
    if existing:
        return QueueResult(True, existing["execution_id"], "job already has an active execution")

    eligibility = evaluate_executor_eligibility(job)
    if not eligibility.enters_queue:
        if eligibility.hard_skip:
            update_job(job_id, notes=(job.notes + " " if job.notes else "") + "; ".join(eligibility.reasons))
        return QueueResult(False, None, "; ".join(eligibility.reasons) or "not eligible")

    dup = duplicate.check_duplicate(job)
    if dup.is_duplicate:
        update_job(job_id, application_state=ApplicationState.DUPLICATE_APPLICATION_BLOCKED)
        record_state_change(job_id, job.application_state.value, ApplicationState.DUPLICATE_APPLICATION_BLOCKED.value,
                             actor="executor")
        return QueueResult(False, None, f"duplicate: {dup.reason} (job {dup.duplicate_job_id})")

    try:
        execution_id = repo.create_execution(job_id, provider=job.provider or "", mode=mode.value)
    except repo.DuplicateExecutionError:
        existing = repo.get_active_execution_for_job(job_id)
        return QueueResult(True, existing["execution_id"] if existing else None, "race: execution already existed")

    return QueueResult(True, execution_id, "queued")


def _profile_fingerprint() -> str:
    profile = load_profile()
    return hashlib.sha256(profile.model_dump_json().encode("utf-8")).hexdigest()[:16]


def _verify_resume_artifact(job: Job, execution: dict) -> tuple[bool, str, str]:
    """CLAUDE.md Phase 8 section 19. Returns (ok, reason, hash)."""
    path_str = job.resume_pdf_path
    if not path_str:
        return False, "no resume artifact on job", ""
    path = Path(path_str)
    if not path.exists():
        return False, f"resume artifact missing on disk: {path}", ""
    if path.parent.name != str(job.id):
        return False, f"resume artifact path '{path}' does not correspond to job_id {job.id}", ""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    stored_hash = execution.get("resume_artifact_hash")
    if stored_hash and stored_hash != digest:
        return False, "resume artifact hash changed since this execution began -- reconciliation required", digest
    return True, "", digest


def _auto_submit_permitted(job: Job, mode, eligibility, provider, validation) -> tuple[bool, str]:
    from app.applications.models import AutomationPolicy

    if not config.APPLICATION_EXECUTOR_ENABLED or not config.AUTO_SUBMIT_ENABLED:
        return False, "executor/auto-submit disabled"
    if mode != ExecutionMode.AUTO_PERMITTED:
        return False, "execution mode is ASSIST"
    if not eligibility.auto_submit_eligible:
        return False, "job not auto-submit eligible (employment type / sponsorship / match / answers)"
    if not provider.capabilities.submission_supported:
        return False, "provider does not support automated submission"
    if not validation.ok or validation.policy != AutomationPolicy.PERMITTED_AUTO:
        return False, "form validation did not clear for automated submission"
    return True, "all AUTO_PERMITTED conditions met"


def process_execution(execution_id: str) -> dict:
    execution = repo.get_execution(execution_id)
    if execution is None:
        raise ValueError(f"execution {execution_id} not found")

    job_id = execution["job_id"]
    job = get_job(job_id)
    if job is None:
        raise ValueError(f"job {job_id} not found")

    if execution["active"] != 1:
        return execution  # already terminal -- no-op, matches idempotent-retry safety

    if execution["status"] == ExecutionStatus.SUBMISSION_STATUS_UNKNOWN.value:
        # CLAUDE.md Phase 8 sections 33/36: an unknown submission outcome is
        # NEVER blindly retried by re-running this function -- it stays
        # exactly as-is until app.applications.reconcile.reconcile_execution()
        # (an explicit human/operator action) resolves it.
        return execution

    correlation_id = job.correlation_id or execution.get("correlation_id") or ""
    mode = ExecutionMode(execution["mode"])

    repo.update_execution(execution_id, job_id, ExecutionStatus.STARTED)
    repo.log_event(execution_id, job_id, "started", correlation_id=correlation_id)

    eligibility = evaluate_executor_eligibility(job)
    if not eligibility.enters_queue:
        repo.update_execution(execution_id, job_id, ExecutionStatus.PERMANENT_SUBMISSION_FAILURE,
                               error_type="NOT_ELIGIBLE", error_message_safe="; ".join(eligibility.reasons)[:500])
        repo.log_event(execution_id, job_id, "failed", detail="not eligible", correlation_id=correlation_id)
        return repo.get_execution(execution_id)

    ok, reason, digest = _verify_resume_artifact(job, execution)
    if not ok:
        repo.update_execution(execution_id, job_id, ExecutionStatus.VALIDATION_REQUIRED,
                               requires_user_action=1, user_action_reason=reason, resume_artifact_hash=digest)
        repo.log_event(execution_id, job_id, "user_action_required", detail=reason, correlation_id=correlation_id)
        return repo.get_execution(execution_id)
    if not execution.get("resume_artifact_hash"):
        repo.update_execution(execution_id, job_id, ExecutionStatus.STARTED,
                               resume_artifact_path=job.resume_pdf_path, resume_artifact_hash=digest)

    profile = load_profile()
    fields = build_application_fields(profile, resume_path=job.resume_pdf_path or "",
                                       cover_letter_path=job.cover_letter_path or "")
    source_version = _profile_fingerprint()
    answers_version = repo.snapshot_answers(execution_id, fields, source_version=source_version)
    repo.log_event(execution_id, job_id, "form_mapped", detail=f"{answers_version} fields snapshotted",
                    correlation_id=correlation_id)

    provider = get_application_provider(job)
    form = provider.discover_form(job)
    fingerprint = form.fingerprint if form else ""
    repo.update_execution(execution_id, job_id, ExecutionStatus.FORM_DISCOVERED, form_fingerprint=fingerprint,
                           answers_version=answers_version, provider=provider.name)
    repo.log_event(execution_id, job_id, "form_discovered" if form else "form_discovery_unsupported",
                    detail=f"provider={provider.name}", correlation_id=correlation_id)

    if form is not None:
        baseline_ok = check_and_record_baseline(form)
        if not baseline_ok:
            repo.update_execution(execution_id, job_id, ExecutionStatus.NEEDS_USER_ACTION,
                                   requires_user_action=1, user_action_reason=PolicyReason.FORM_SCHEMA_CHANGED.value,
                                   automation_policy="", policy_reasons=json.dumps([PolicyReason.FORM_SCHEMA_CHANGED.value]))
            repo.log_event(execution_id, job_id, "user_action_required", detail="FORM_SCHEMA_CHANGED",
                            correlation_id=correlation_id)
            return repo.get_execution(execution_id)

        mapping = provider.map_fields(form, fields)
        repo.log_event(execution_id, job_id, "form_mapped", detail=f"mapped={len(mapping.mapped)}",
                        correlation_id=correlation_id)
        draft = provider.fill_draft(form, mapping)
    else:
        from app.applications.models import DraftResult, MappingResult

        mapping = MappingResult()
        draft = DraftResult(mapping=mapping, preserved=False)

    repo.update_execution(execution_id, job_id, ExecutionStatus.FORM_FILLED)
    repo.log_event(execution_id, job_id, "filled", detail=f"filled={len(draft.filled_field_ids)}",
                    correlation_id=correlation_id)

    validation = provider.validate(job, form, draft)
    policy_reasons_json = json.dumps([r.value for r in validation.policy_reasons])

    if not validation.ok:
        repo.update_execution(execution_id, job_id, ExecutionStatus.NEEDS_USER_ACTION,
                               requires_user_action=1,
                               user_action_reason="; ".join(validation.detail)[:500],
                               automation_policy=validation.policy.value, policy_reasons=policy_reasons_json)
        repo.log_event(execution_id, job_id, "user_action_required",
                        detail=";".join(r.value for r in validation.policy_reasons), correlation_id=correlation_id)
        return repo.get_execution(execution_id)

    auto_ok, auto_reason = _auto_submit_permitted(job, mode, eligibility, provider, validation)
    if not auto_ok:
        repo.update_execution(execution_id, job_id, ExecutionStatus.SUBMISSION_READY,
                               requires_user_action=1, user_action_reason=auto_reason,
                               automation_policy=validation.policy.value, policy_reasons=policy_reasons_json)
        repo.log_event(execution_id, job_id, "validated", detail=auto_reason, correlation_id=correlation_id)
        return repo.get_execution(execution_id)

    rl = rate_limit.check_rate_limits(job.company)
    if not rl.allowed:
        repo.update_execution(execution_id, job_id, ExecutionStatus.NEEDS_USER_ACTION,
                               requires_user_action=1, user_action_reason=rl.reason,
                               automation_policy=validation.policy.value, policy_reasons=policy_reasons_json)
        repo.log_event(execution_id, job_id, "user_action_required", detail=rl.reason, correlation_id=correlation_id)
        return repo.get_execution(execution_id)

    dup = duplicate.check_duplicate(job)
    if dup.is_duplicate:
        repo.update_execution(execution_id, job_id, ExecutionStatus.DUPLICATE_APPLICATION_BLOCKED,
                               error_type="DUPLICATE", error_message_safe=dup.reason)
        repo.log_event(execution_id, job_id, "failed", detail="duplicate at submit time", correlation_id=correlation_id)
        return repo.get_execution(execution_id)

    repo.update_execution(execution_id, job_id, ExecutionStatus.SUBMITTING,
                           attempt_count=execution["attempt_count"] + 1,
                           automation_policy=validation.policy.value, policy_reasons=policy_reasons_json)
    repo.log_event(execution_id, job_id, "submit_attempted", detail=f"provider={provider.name}",
                    correlation_id=correlation_id)

    result = provider.submit(job, form, draft)

    if result.status_unknown:
        repo.update_execution(execution_id, job_id, ExecutionStatus.SUBMISSION_STATUS_UNKNOWN,
                               requires_user_action=1,
                               user_action_reason="submission outcome unknown -- reconcile before retrying",
                               error_type=result.error_type, error_message_safe=result.error_message_safe,
                               submission_method=provider.name)
        repo.log_event(execution_id, job_id, "failed", detail="SUBMISSION_STATUS_UNKNOWN", correlation_id=correlation_id)
        return repo.get_execution(execution_id)

    if not result.success:
        repo.update_execution(execution_id, job_id, ExecutionStatus.PERMANENT_SUBMISSION_FAILURE,
                               error_type=result.error_type, error_message_safe=result.error_message_safe,
                               submission_method=provider.name)
        repo.log_event(execution_id, job_id, "failed", detail=result.error_type, correlation_id=correlation_id)
        return repo.get_execution(execution_id)

    repo.update_execution(execution_id, job_id, ExecutionStatus.SUBMITTED, submission_method=provider.name)
    confirmation = provider.verify_confirmation(result)
    if not confirmation.confirmed:
        repo.update_execution(execution_id, job_id, ExecutionStatus.SUBMISSION_STATUS_UNKNOWN,
                               requires_user_action=1,
                               user_action_reason="submit reported success but no confirmation evidence found")
        repo.log_event(execution_id, job_id, "failed", detail="no confirmation evidence", correlation_id=correlation_id)
        return repo.get_execution(execution_id)

    repo.update_execution(execution_id, job_id, ExecutionStatus.APPLIED,
                           confirmation_id=confirmation.confirmation_id,
                           confirmation_url=confirmation.confirmation_url,
                           confirmation_text_fingerprint=confirmation.confirmation_text_fingerprint)
    repo.log_event(execution_id, job_id, "confirmed", detail=confirmation.confirmation_id,
                    correlation_id=correlation_id)
    return repo.get_execution(execution_id)
