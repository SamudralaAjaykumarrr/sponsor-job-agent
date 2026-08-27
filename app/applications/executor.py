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
from app.applications import approval, duplicate, rate_limit, receipts, repo
from app.applications import blockers, document_binding
from app.applications.eligibility import evaluate_executor_eligibility
from app.applications.fingerprint import check_and_record_baseline
from app.applications.models import (
    AutomationPolicy,
    ExecutionMode,
    ExecutionStatus,
    PolicyReason,
    SUBMIT_RETRYABLE_ERROR_TYPES,
)
from app.applications.provider_registry import get_application_provider
from app.applications.schema import build_application_fields
from app.candidate.profile import load_profile
from app.jobs_repo import get_job, record_state_change, update_job
from app.models import ApplicationState, Job, SponsorshipStatus


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
    """CLAUDE.md Phase 8 section 19. Returns (ok, reason, hash).

    A resume artifact's path either matches the legacy flat layout
    (`output/<job_id>/resume.pdf`, immediate parent dir == job_id) or the
    resume_optimizer's nested one-page-variant layout (`output/<job_id>/
    optimized/<variant_id>/resume.pdf`, promoted by app.agent.orchestrator.
    _run_resume_stage) -- checking the `/<job_id>/` path segment appears
    anywhere, matching the exact convention app.applications.doctor.
    _check_wrong_resume_job_mapping and app.resume_optimizer.doctor.
    _check_resume_linked_to_wrong_job already use for this same question, so
    all three 'does this resume belong to this job' checks in this project
    agree. A real integration gap between the Phase 8 executor and the Phase
    14+ optimizer's nested paths, caught live once resume promotion started
    exercising both together -- never re-narrow this back to an exact
    immediate-parent-name match."""
    path_str = job.resume_pdf_path
    if not path_str:
        return False, "no resume artifact on job", ""
    path = Path(path_str)
    if not path.exists():
        return False, f"resume artifact missing on disk: {path}", ""
    normalized = str(path).replace("\\", "/")
    if f"/{job.id}/" not in normalized:
        return False, f"resume artifact path '{path}' does not correspond to job_id {job.id}", ""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    stored_hash = execution.get("resume_artifact_hash")
    if stored_hash and stored_hash != digest:
        return False, "resume artifact hash changed since this execution began -- reconciliation required", digest
    return True, "", digest


_DOCUMENT_KIND_BY_FIELD_ID = {
    "resume_file": document_binding.DocumentKind.RESUME,
    "cover_letter_file": document_binding.DocumentKind.COVER_LETTER,
}


def _record_document_selection_bindings(job: Job, execution_id: str, provider_name: str, mapping,
                                         form_fingerprint: str) -> list[dict]:
    """Real Provider Execution V1 (the brief's DOCUMENT UPLOAD requirement,
    headless/draft half): records WHICH generated artifact was SELECTED for
    WHICH provider upload field on this execution.

    Deliberately `verified=0`: this path prepares a draft, it never performs
    a network upload (no real provider in this project has a tested
    submission interface), so claiming a verified upload here would be
    exactly the kind of inflated evidence CLAUDE.md forbids. The
    browser-assist path records `verified=1` bindings, because there the
    file genuinely was accepted by a live form field. Best-effort: an
    audit-log failure never fails an otherwise-good preparation."""
    recorded: list[dict] = []
    if job.id is None:
        return recorded
    for m in mapping.mapped:
        app_field = m.application_field
        if app_field is None or not m.will_fill:
            continue
        kind = _DOCUMENT_KIND_BY_FIELD_ID.get(app_field.field_id)
        if kind is None:
            continue
        artifact_path = app_field.verified_value or ""
        check = document_binding.verify_artifact_matches_job(job.id, artifact_path)
        row = document_binding.record_binding_safe(
            job_id=job.id, document_kind=kind, artifact_path=artifact_path, provider=provider_name,
            execution_id=execution_id, provider_field_id=m.form_field.name or "",
            provider_field_label=m.form_field.label or "",
            resume_variant_id=job.promoted_resume_variant_id or "",
            checkpoint=f"executor:form_fingerprint={form_fingerprint[:16]}" if form_fingerprint else "executor",
            verified=False, artifact_sha256=check.sha256,
            detail=check.reason or "selected for upload during draft preparation (no network upload performed)",
        )
        if row is not None:
            recorded.append(row)
    return recorded


def _auto_submit_permitted(job: Job, mode, eligibility, provider, validation) -> tuple[bool, str]:
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


def _approved_submit_permitted(job: Job, eligibility, provider, validation, execution: dict) -> tuple[bool, str]:
    """Approval-gated-autonomy-v1 (see app.applications.approval): the ONLY
    other gate, besides `_auto_submit_permitted` above, that may unlock a
    provider.submit() call -- reached only when a human has already clicked
    APPROVE & APPLY (app.applications.approval.approve_and_apply), never by
    ExecutionMode.AUTO_PERMITTED/AUTO_SUBMIT_ENABLED alone. Deliberately
    does NOT require ExecutionMode.AUTO_PERMITTED or CONFIRMED_SPONSOR-only
    (unlike _auto_submit_permitted's unattended path) -- a human has already
    reviewed the READY_FOR_APPROVAL package, including any 'sponsorship
    history found -- verify before applying' warning for LIKELY_SPONSOR, so
    LIKELY_SPONSOR may proceed here. UNKNOWN/NO_SPONSORSHIP can never reach
    this point at all (both already block enters_queue/hard_skip upstream).
    Still requires a genuinely tested, verified provider capability
    (provider.capabilities.submission_supported) and a clean, unblocked
    validation -- approval never fakes/forces a submission capability that
    doesn't exist, and never bypasses CAPTCHA/MFA/login/legal blockers.

    The FIRST check is the server-side durable-approval gate
    (app.applications.approval.verify_durable_approval_for_submission) --
    this never trusts the caller having passed `approved=True` into
    process_execution() as sufficient evidence by itself; it re-fetches the
    latest application_approvals row fresh from the database and
    re-validates it against `execution`'s CURRENT fingerprints. A missing,
    non-ACTIVE, or stale approval always blocks here, regardless of what
    `approved` was set to.

    Deliberately does NOT re-check config.APPLICATION_EXECUTOR_ENABLED --
    that flag gates creating a NEW execution (app.applications.executor.
    queue_application's ExecutorDisabledError) and is temporarily raised by
    the orchestrator only while it is actively RUNNING; CONTINUING an
    already-existing, already-queued execution via an explicit human
    approval action has never been gated by it anywhere in this codebase
    (process_execution() itself has no such check either -- see e.g. the
    Retry Preparation button), and requiring it here would mean approving a
    job the agent already prepared silently stops working the moment a
    user clicks STOP AGENT, which is not the intended product behavior."""
    approved_ok, approved_reason = approval.verify_durable_approval_for_submission(job, execution)
    if not approved_ok:
        return False, approved_reason
    if eligibility.hard_skip or not eligibility.enters_queue:
        return False, "job is no longer eligible"
    if job.sponsorship_status not in (SponsorshipStatus.CONFIRMED_SPONSOR, SponsorshipStatus.LIKELY_SPONSOR):
        return False, "sponsorship status no longer permits submission"
    if not provider.capabilities.submission_supported:
        return False, "provider has no verified final-submission capability -- use browser assist or complete manually"
    if not validation.ok or validation.policy != AutomationPolicy.PERMITTED_AUTO:
        return False, "form validation did not clear for submission"
    return True, "human-approved submission permitted"


def process_execution(execution_id: str, *, allow_submission: bool = True, approved: bool = False,
                       attempt_id: str = "") -> dict:
    """`allow_submission=False` (CLAUDE.md Phase 9 section 13, drain mode):
    runs the full prepare/map/fill/validate pipeline exactly as normal but
    never calls provider.submit(), landing in SUBMISSION_READY instead --
    used by a draining application worker that must finish safe in-progress
    preparation but never start a new submission.

    `approved=True` (Approval-gated-autonomy-v1): set only by
    app.applications.approval.approve_and_apply() immediately after
    recording a durable, fingerprint-bound approval record. Re-runs this
    exact same pipeline (never a fork/shortcut) so every gate --
    eligibility, resume-artifact hash, form/answers, job-still-active,
    rate limits, duplicates -- is revalidated fresh, then additionally
    tries `_approved_submit_permitted` if the ordinary unattended
    `_auto_submit_permitted` gate doesn't already clear it."""
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

    if execution["status"] in (ExecutionStatus.SUBMITTING.value, ExecutionStatus.SUBMITTED.value):
        # CLAUDE.md Phase 9 sections 7/33 (acceptance scenario E): a worker
        # that crashed (killed, network partition) after writing SUBMITTING
        # but before recording a final outcome left an execution whose
        # provider.submit() call may or may not have actually reached the
        # provider. Resuming this function from scratch would call submit()
        # a SECOND time -- a real double-submission risk this exact code
        # path used to have no guard against. Never blindly retry: convert
        # straight to SUBMISSION_STATUS_UNKNOWN so only explicit
        # reconciliation (human, or app.applications.reconcile_worker's
        # genuine-evidence path) can resolve it.
        correlation_id = job.correlation_id or execution.get("correlation_id") or ""
        repo.update_execution(execution_id, job_id, ExecutionStatus.SUBMISSION_STATUS_UNKNOWN,
                               requires_user_action=1,
                               user_action_reason="execution resumed while in-flight after an interruption -- "
                                                   "submission outcome unknown, reconciliation required")
        repo.log_event(execution_id, job_id, "failed", detail="resumed_mid_submission", correlation_id=correlation_id)
        blockers.raise_blocker(execution_id, job_id, blockers.BlockerCode.SUBMISSION_STATUS_UNKNOWN,
                                provider=execution.get("provider") or "", attempt_id=attempt_id,
                                detail="execution resumed while in-flight -- submission outcome unknown",
                                source="executor.resumed_mid_submission")
        return repo.get_execution(execution_id)

    if execution["status"] in (ExecutionStatus.NEEDS_USER_ACTION.value, ExecutionStatus.VALIDATION_REQUIRED.value):
        # Application-lifecycle-exception-resume-v1: process_execution()
        # always fully re-validates every gate on every call, so a resume
        # attempt (Retry Preparation / the user resolved the blocker
        # out-of-band) resolves the prior blocker up front -- if the same
        # condition still holds, the exact same code is raised again a few
        # lines below as a fresh row, giving an honest append-only history
        # rather than one row silently mutated in place.
        blockers.resolve_blocker(execution_id, resolution_note="reprocessing attempted")

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
        blockers.raise_blocker(execution_id, job_id, blockers.BlockerCode.NEEDS_USER_INPUT, detail=reason,
                                attempt_id=attempt_id, source="executor.resume_claim_check")
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
            blockers.raise_blocker(execution_id, job_id, blockers.BlockerCode.APPLICATION_ERROR,
                                    provider=provider.name, detail="application form schema changed unexpectedly",
                                    attempt_id=attempt_id, source="executor.form_schema_changed")
            return repo.get_execution(execution_id)

        mapping = provider.map_fields(form, fields)
        repo.log_event(execution_id, job_id, "form_mapped", detail=f"mapped={len(mapping.mapped)}",
                        correlation_id=correlation_id)
        draft = provider.fill_draft(form, mapping)
        _record_document_selection_bindings(job, execution_id, provider.name, mapping, fingerprint)
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
        code = blockers.from_policy_reasons(validation.policy_reasons)
        if code is not None:
            blockers.raise_blocker(
                execution_id, job_id, code, provider=provider.name,
                detail="; ".join(validation.detail)[:2000], attempt_id=attempt_id,
                resume_checkpoint={"execution_id": execution_id, "form_fingerprint": fingerprint,
                                    "answers_version": answers_version},
                source="executor.validation_failed",
            )
        return repo.get_execution(execution_id)

    if not allow_submission:
        # CLAUDE.md Phase 9 section 13 (drain mode): finish safe in-progress
        # preparation, but never start a new submission -- the fully
        # prepared draft is preserved exactly as a normal ASSIST review item.
        repo.update_execution(execution_id, job_id, ExecutionStatus.SUBMISSION_READY,
                               requires_user_action=1, user_action_reason="worker draining -- submission deferred",
                               automation_policy=validation.policy.value, policy_reasons=policy_reasons_json)
        repo.log_event(execution_id, job_id, "validated", detail="draining_defer_submission", correlation_id=correlation_id)
        return repo.get_execution(execution_id)

    # --- CLAUDE.md Phase 9 sections 24-27: revalidate immediately before
    # submission using a FRESH read of the job -- time may have passed since
    # eligibility was first checked at the top of this call (form discovery/
    # mapping can involve real network requests), and a discovery cycle may
    # have reanalyzed this job (e.g. a JD edit flipping sponsorship negative)
    # in the meantime. Never submit against stale eligibility state.
    fresh_job = get_job(job_id)
    if fresh_job is None:
        repo.update_execution(execution_id, job_id, ExecutionStatus.JOB_NO_LONGER_ACTIVE,
                               error_type="JOB_NO_LONGER_ACTIVE", error_message_safe="job no longer exists")
        repo.log_event(execution_id, job_id, "failed", detail="job_missing_before_submit", correlation_id=correlation_id)
        blockers.raise_blocker(execution_id, job_id, blockers.BlockerCode.JOB_REMOVED, provider=provider.name,
                                detail="job no longer exists in our records", attempt_id=attempt_id,
                                source="executor.job_missing_before_submit")
        return repo.get_execution(execution_id)

    still_active = provider.check_job_still_active(fresh_job)
    if still_active is False:
        repo.update_execution(execution_id, job_id, ExecutionStatus.JOB_NO_LONGER_ACTIVE,
                               error_type="JOB_NO_LONGER_ACTIVE",
                               error_message_safe="provider reports this posting is no longer active")
        repo.log_event(execution_id, job_id, "failed", detail="job_inactive_before_submit", correlation_id=correlation_id)
        inactive_code = blockers.from_job_inactive_reason(provider.classify_job_inactive_reason(fresh_job))
        blockers.raise_blocker(execution_id, job_id, inactive_code, provider=provider.name,
                                detail="provider reports this posting is no longer active", attempt_id=attempt_id,
                                source="executor.job_inactive_before_submit")
        return repo.get_execution(execution_id)

    fresh_eligibility = evaluate_executor_eligibility(fresh_job)
    if fresh_eligibility.hard_skip:
        # Covers, among others, "JD changed to no sponsorship" / employment
        # type flipped to a hard-skip category since preparation began --
        # always a hard stop, never a submission.
        repo.update_execution(execution_id, job_id, ExecutionStatus.JOB_NO_LONGER_ACTIVE,
                               error_type="REVALIDATION_HARD_SKIP",
                               error_message_safe="; ".join(fresh_eligibility.reasons)[:500])
        repo.log_event(execution_id, job_id, "failed", detail="revalidation_hard_skip", correlation_id=correlation_id)
        return repo.get_execution(execution_id)

    auto_ok, auto_reason = _auto_submit_permitted(fresh_job, mode, fresh_eligibility, provider, validation)
    if not auto_ok and approved:
        # A fresh read of the execution row -- `execution` (fetched at the
        # top of this call) is stale by now: form_fingerprint/answers_version
        # were written by the FORM_DISCOVERED/snapshot_answers steps above,
        # in THIS same call, and never reflected back onto the local
        # `execution` dict. The durable-approval gate inside
        # _approved_submit_permitted must compare against what is actually
        # about to be submitted, not a pre-pipeline snapshot.
        current_execution = repo.get_execution(execution_id)
        auto_ok, auto_reason = _approved_submit_permitted(fresh_job, fresh_eligibility, provider, validation,
                                                            current_execution)
    if not auto_ok:
        # Approved but this specific provider has no verified submission
        # capability (or a blocker reappeared on revalidation): land on the
        # honest APPROVED resting state -- never silently fall back to the
        # generic "needs your action" SUBMISSION_READY/requires_user_action
        # framing, since the human has already reviewed and approved this
        # application (CLAUDE.md approval spec section 8).
        status = ExecutionStatus.APPROVED if approved else ExecutionStatus.SUBMISSION_READY
        repo.update_execution(execution_id, job_id, status,
                               requires_user_action=0 if approved else 1, user_action_reason=auto_reason,
                               automation_policy=validation.policy.value, policy_reasons=policy_reasons_json)
        repo.log_event(execution_id, job_id, "validated", detail=auto_reason, correlation_id=correlation_id)
        return repo.get_execution(execution_id)

    rl = rate_limit.check_rate_limits(fresh_job.company)
    if not rl.allowed:
        repo.update_execution(execution_id, job_id, ExecutionStatus.NEEDS_USER_ACTION,
                               requires_user_action=1, user_action_reason=rl.reason,
                               automation_policy=validation.policy.value, policy_reasons=policy_reasons_json)
        repo.log_event(execution_id, job_id, "user_action_required", detail=rl.reason, correlation_id=correlation_id)
        return repo.get_execution(execution_id)

    dup = duplicate.check_duplicate(fresh_job)
    if dup.is_duplicate:
        repo.update_execution(execution_id, job_id, ExecutionStatus.DUPLICATE_APPLICATION_BLOCKED,
                               error_type="DUPLICATE", error_message_safe=dup.reason)
        repo.log_event(execution_id, job_id, "failed", detail="duplicate at submit time", correlation_id=correlation_id)
        return repo.get_execution(execution_id)

    attempt_count_now = execution["attempt_count"] + 1
    repo.update_execution(execution_id, job_id, ExecutionStatus.SUBMITTING,
                           attempt_count=attempt_count_now,
                           automation_policy=validation.policy.value, policy_reasons=policy_reasons_json)
    repo.log_event(execution_id, job_id, "submit_attempted", detail=f"provider={provider.name}",
                    correlation_id=correlation_id)

    result = provider.submit(fresh_job, form, draft)

    if result.status_unknown:
        repo.update_execution(execution_id, job_id, ExecutionStatus.SUBMISSION_STATUS_UNKNOWN,
                               requires_user_action=1,
                               user_action_reason="submission outcome unknown -- reconcile before retrying",
                               error_type=result.error_type, error_message_safe=result.error_message_safe,
                               submission_method=provider.name)
        repo.log_event(execution_id, job_id, "failed", detail="SUBMISSION_STATUS_UNKNOWN", correlation_id=correlation_id)
        blockers.raise_blocker(execution_id, job_id, blockers.BlockerCode.SUBMISSION_STATUS_UNKNOWN,
                                provider=provider.name, detail=result.error_message_safe or "",
                                attempt_id=attempt_id, source="executor.submit_status_unknown")
        return repo.get_execution(execution_id)

    if not result.success:
        # Autonomous-ux-reliability-v1: a submit failure the provider
        # affirmatively reported as not-yet-processed (rate limit / temporary
        # HTTP error -- never an ambiguous status_unknown outcome, already
        # handled above and always excluded here) gets a bounded number of
        # retries with exponential backoff before being treated as
        # permanent. Every retry is a fresh call to process_execution()
        # (via the worker reclaiming the execution once its backoff lease
        # expires -- see app.applications.queue/app.applications.worker),
        # so identity/eligibility/approval/resume-hash/form-fingerprint are
        # always revalidated from scratch, never reused stale.
        retryable_error = result.error_type in SUBMIT_RETRYABLE_ERROR_TYPES
        if retryable_error and attempt_count_now < config.APPLICATION_SUBMIT_RETRY_MAX_ATTEMPTS:
            repo.update_execution(execution_id, job_id, ExecutionStatus.RETRYABLE_SUBMISSION_FAILURE,
                                   error_type=result.error_type, error_message_safe=result.error_message_safe,
                                   submission_method=provider.name)
            repo.log_event(execution_id, job_id, "failed",
                            detail=f"retryable_submit_failure:{result.error_type} attempt={attempt_count_now}",
                            correlation_id=correlation_id)
            return repo.get_execution(execution_id)

        repo.update_execution(execution_id, job_id, ExecutionStatus.PERMANENT_SUBMISSION_FAILURE,
                               error_type=result.error_type, error_message_safe=result.error_message_safe,
                               submission_method=provider.name)
        repo.log_event(execution_id, job_id, "failed", detail=result.error_type, correlation_id=correlation_id)
        if retryable_error:
            # Retries exhausted -- park as an issue for a human; never
            # retried again automatically (blocker is informational only,
            # execution is already terminal via PERMANENT_SUBMISSION_FAILURE
            # above).
            blockers.raise_blocker(
                execution_id, job_id, blockers.BlockerCode.APPLICATION_ERROR, provider=provider.name,
                detail=f"submission failed after {attempt_count_now} attempts ({result.error_type}): "
                       f"{result.error_message_safe}",
                attempt_id=attempt_id, source="executor.submit_retry_exhausted",
            )
        return repo.get_execution(execution_id)

    repo.update_execution(execution_id, job_id, ExecutionStatus.SUBMITTED, submission_method=provider.name)
    confirmation = provider.verify_confirmation(result)
    if not confirmation.confirmed:
        repo.update_execution(execution_id, job_id, ExecutionStatus.SUBMISSION_STATUS_UNKNOWN,
                               requires_user_action=1,
                               user_action_reason="submit reported success but no confirmation evidence found")
        repo.log_event(execution_id, job_id, "failed", detail="no confirmation evidence", correlation_id=correlation_id)
        blockers.raise_blocker(execution_id, job_id, blockers.BlockerCode.SUBMISSION_STATUS_UNKNOWN,
                                provider=provider.name,
                                detail="submit reported success but no confirmation evidence found",
                                attempt_id=attempt_id, source="executor.no_confirmation_evidence")
        return repo.get_execution(execution_id)

    blockers.resolve_blocker(execution_id, resolution_note="application confirmed")
    repo.update_execution(execution_id, job_id, ExecutionStatus.APPLIED,
                           confirmation_id=confirmation.confirmation_id,
                           confirmation_url=confirmation.confirmation_url,
                           confirmation_text_fingerprint=confirmation.confirmation_text_fingerprint)
    repo.log_event(execution_id, job_id, "confirmed", detail=confirmation.confirmation_id,
                    correlation_id=correlation_id)
    _record_receipt_best_effort(
        execution_id=execution_id, job_id=job_id, provider=provider.name,
        submitted_via=f"headless_provider:{provider.name}", confirmation_id=confirmation.confirmation_id,
        sanitized_url=confirmation.confirmation_url,
        evidence_strength="STRONG" if confirmation.confirmation_id else "MODERATE",
        raw_message_fingerprint=confirmation.confirmation_text_fingerprint,
    )
    return repo.get_execution(execution_id)


def _record_receipt_best_effort(**kwargs) -> None:
    """Receipts are durable evidence, never a gate -- a failure recording one
    must never turn an already-genuinely-confirmed APPLIED execution into an
    error (mirrors app.applications.checkpoints/spa_events' own
    best-effort-observability contract)."""
    try:
        latest_approval = approval.get_latest_approval(kwargs["execution_id"])
        approval_id = latest_approval["approval_id"] if latest_approval else ""
        receipts.record_receipt(approval_id=approval_id, **kwargs)
    except Exception:  # noqa: BLE001
        pass
