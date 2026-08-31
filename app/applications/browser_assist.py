"""Visible browser-assist layer (CLAUDE.md Phase 9 sections 21-23). OPTIONAL:
requires the `playwright` package AND its browser binaries
(`playwright install chromium`) installed separately -- never a default or
implicit dependency, and BROWSER_ASSIST_ENABLED defaults to False so nothing
here runs unless explicitly opted into.

Strict boundaries this module never crosses:
  - No stealth plugins, no browser-fingerprint spoofing, no CAPTCHA solving,
    no proxy rotation, no anti-bot bypass, no hidden/automated login, no MFA
    interception.
  - Never persists passwords, MFA codes, long-lived cookies, or auth tokens.
    Every browser context is a fresh, ephemeral, non-persistent
    `browser.new_context()` (never `launch_persistent_context()` with a
    reused profile directory, never `storage_state` saved to disk), closed
    at the end of every call regardless of outcome.
  - NEVER clicks a final "submit"/"apply" action, ever, under any
    condition -- this module only opens the page, detects fields (reusing
    the exact same deterministic app.applications.mapping engine every
    other provider adapter uses -- never a second, different matching
    heuristic), fills verified NON-sensitive values, prepares a resume/cover
    letter file upload, and stops the moment it hits a CAPTCHA, a
    login/MFA wall, or any field it cannot safely resolve.

Honest limitation (CLAUDE.md Phase 9 section 57): this MVP closes the
browser before returning a HandoffRecord -- it does not (yet) keep a visible
window open for the candidate to continue in-place. The HandoffRecord always
carries the real application URL so the candidate can open it themselves."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app import config
from app.applications.mapping import match_field, match_field_with_application_fields
from app.applications.models import ApplicationField, FieldCategory, FieldConfidence, SENSITIVE_CATEGORIES
from app.models import Job


class BrowserAssistUnavailable(Exception):
    """Raised (never silently swallowed) when BROWSER_ASSIST_ENABLED is
    False, or when Playwright / its browser binaries aren't installed."""


@dataclass
class HandoffRecord:
    """CLAUDE.md Phase 9 section 23: exactly what a NEEDS_USER_ACTION
    review queue entry should show."""
    job_id: int
    company: str
    application_url: str
    stage: str
    reason: str
    prepared_field_ids: list[str] = field(default_factory=list)
    unresolved_field_ids: list[str] = field(default_factory=list)
    resume_path: str = ""
    cover_letter_path: str = ""

    def as_dict(self) -> dict:
        return {
            "job_id": self.job_id, "company": self.company, "application_url": self.application_url,
            "stage": self.stage, "reason": self.reason, "prepared_field_ids": self.prepared_field_ids,
            "unresolved_field_ids": self.unresolved_field_ids, "resume_path": self.resume_path,
            "cover_letter_path": self.cover_letter_path,
        }


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def _require_playwright() -> None:
    if not config.BROWSER_ASSIST_ENABLED:
        raise BrowserAssistUnavailable("BROWSER_ASSIST_ENABLED is false.")
    if not playwright_available():
        raise BrowserAssistUnavailable(
            "playwright is not installed -- run `pip install playwright && playwright install chromium`."
        )


def _detect_fields(page) -> list[dict]:
    """Best-effort DOM scan for visible text/textarea/select/file inputs and
    their associated label text. Never touches password/hidden/checkbox/
    radio/submit/button inputs -- those are always left for the human."""
    return page.evaluate(
        """
        () => {
          const results = [];
          document.querySelectorAll('input, textarea, select').forEach((el, idx) => {
            const type = (el.getAttribute('type') || el.tagName).toLowerCase();
            if (['hidden', 'password', 'submit', 'button', 'checkbox', 'radio'].includes(type)) return;
            let label = '';
            if (el.labels && el.labels.length) label = el.labels[0].innerText;
            if (!label) label = el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.name || '';
            results.push({index: idx, label: (label || '').trim(), type: type, name: el.name || '', id: el.id || ''});
          });
          return results;
        }
        """
    )


def _selector_for(raw_field: dict) -> str:
    if raw_field["id"]:
        return f"#{raw_field['id']}"
    if raw_field["name"]:
        return f"[name='{raw_field['name']}']"
    return f":nth-match(input, textarea, select, {raw_field['index'] + 1})"


def prepare_application(
    job: Job, application_fields: list[ApplicationField], *, resume_path: str = "", cover_letter_path: str = "",
) -> HandoffRecord:
    """Opens the job's application URL in a browser (visible unless
    BROWSER_ASSIST_HEADLESS), detects fields, fills verified non-sensitive
    values, prepares a file upload, and stops at any user-required action.
    Raises BrowserAssistUnavailable if the feature/dependency isn't
    available -- never silently no-ops."""
    _require_playwright()
    from playwright.sync_api import sync_playwright

    url = job.canonical_url or job.url
    if not url:
        return HandoffRecord(
            job_id=job.id or 0, company=job.company, application_url="", stage="FORM_NOT_FOUND",
            reason="UNSUPPORTED_SUBMISSION", resume_path=resume_path, cover_letter_path=cover_letter_path,
        )

    prepared: list[str] = []
    unresolved: list[str] = []
    stage = "OPENED"
    reason = ""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=config.BROWSER_ASSIST_HEADLESS)
        # Ephemeral context ONLY -- never launch_persistent_context, never a
        # saved storage_state, so no cookie/session/token ever survives past
        # this single call.
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        try:
            page.goto(url, timeout=config.BROWSER_ASSIST_TIMEOUT_SECONDS * 1000)
            lowered = page.content().lower()
            if "captcha" in lowered or page.locator("iframe[src*='captcha']").count() > 0:
                stage, reason = "USER_ACTION_REQUIRED", "CAPTCHA_PRESENT"
            elif page.locator("input[type=password]").count() > 0:
                stage, reason = "USER_ACTION_REQUIRED", "LOGIN_REQUIRED"
            else:
                raw_fields = _detect_fields(page)
                for rf in raw_fields:
                    field_id, confidence = match_field(rf["label"], rf["name"])
                    app_field: Optional[ApplicationField] = (
                        next((f for f in application_fields if f.field_id == field_id), None) if field_id else None
                    )
                    label = rf["label"] or rf["name"] or f"field#{rf['index']}"
                    if app_field is None or not app_field.auto_fill_allowed or app_field.category in SENSITIVE_CATEGORIES:
                        unresolved.append(label)
                        continue
                    if confidence == FieldConfidence.LOW:
                        unresolved.append(label)
                        continue
                    selector = _selector_for(rf)
                    try:
                        if rf["type"] == "file":
                            if app_field.verified_value and Path(app_field.verified_value).exists():
                                page.locator(selector).set_input_files(app_field.verified_value)
                                prepared.append(app_field.field_id)
                            else:
                                unresolved.append(label)
                        else:
                            page.locator(selector).fill(str(app_field.verified_value))
                            prepared.append(app_field.field_id)
                    except Exception:  # noqa: BLE001 -- a single unfillable field must never abort the whole pass
                        unresolved.append(label)
                stage = "DRAFT_READY" if not unresolved else "USER_ACTION_REQUIRED"
                reason = "" if not unresolved else "UNRESOLVED_REQUIRED_FIELD"
        except Exception as exc:  # noqa: BLE001 -- navigation/timeout/DOM errors never crash the caller
            stage, reason = "USER_ACTION_REQUIRED", f"PLATFORM_POLICY_RESTRICTED: {type(exc).__name__}"
        finally:
            context.close()
            browser.close()

    return HandoffRecord(
        job_id=job.id or 0, company=job.company, application_url=url, stage=stage, reason=reason,
        prepared_field_ids=prepared, unresolved_field_ids=unresolved,
        resume_path=resume_path, cover_letter_path=cover_letter_path,
    )


# =============================================================================
# CLAUDE.md Phase 10: production-quality, resumable, session-based browser
# assist. This is the API the executor/worker/dashboard/CLI use going
# forward -- `prepare_application()` above is kept unchanged for backward
# compatibility (it still works exactly as before) but is a one-shot,
# single-page, close-immediately helper; everything below supports a
# persistent visible window across multiple separate calls, multi-step forms,
# pause/resume for login/CAPTCHA/legal questions, crash recovery, and
# post-manual-submit confirmation capture. See docs/browser-assist-sessions.md.
# =============================================================================

import hashlib
import os

from app.applications import approval as _approval
from app.applications import browser_runtime
from app.applications import checkpoints
from app.applications import document_binding
from app.applications import receipts as _receipts
from app.applications import repo as _executions_repo
from app.applications import spa_events
from app.applications.apply_entry import EntryDetectionResult, EntryStage, StepConfidence, is_valid_stage_transition
from app.applications.trusted_redirects import resolve_application_url
from app.applications.browser_session import (
    BrowserPauseReason,
    BrowserSessionStatus,
    DuplicateSessionError,
    PAUSED_STATUSES,
    REASON_TO_STATUS,
    TERMINAL_SESSION_STATUSES,
)
from app.applications import browser_session
from app.applications.eligibility import evaluate_executor_eligibility
from app.applications import resume_integrity
from app.applications.models import ExecutionStatus
from app.applications.schema import build_application_fields, find_field
from app.candidate.profile import load_profile
from app.config import BROWSER_SESSION_LEASE_SECONDS
from app.jobs_repo import get_job

_TERMINAL_SESSION_VALUES = {s.value for s in TERMINAL_SESSION_STATUSES}
_PAUSED_VALUES = {s.value for s in PAUSED_STATUSES}

# CLAUDE.md Phase 11 section 26: stable within THIS process so a single
# orchestration call that internally delegates to another browser_assist
# function (mark_user_action_complete -> resume_session) never conflicts
# with its own lease -- see browser_session.claim_session's re-entrant
# same-worker_id clause. A DIFFERENT process/worker always gets a distinct
# pid-derived id, so real cross-process ownership stays exclusive.
_WORKER_ID = f"proc-{os.getpid()}"

_MAX_APPLY_ENTRY_HOPS = 3


def _claim_or_conflict(session_id: str) -> tuple[bool, dict]:
    """CLAUDE.md Phase 11 sections 26-27: claims exclusive ownership of a
    session for the duration of one orchestration call, released again (see
    each caller's `finally`) the moment that call finishes -- never held
    indefinitely, so any worker can resume a paused session later. Returns
    (owned, session_row); when `owned` is False, the caller must not touch
    the browser at all."""
    claimed = browser_session.claim_session(session_id, worker_id=_WORKER_ID, lease_seconds=BROWSER_SESSION_LEASE_SECONDS)
    if claimed is not None:
        return True, claimed
    current = browser_session.get_session(session_id)
    return False, current or {}


def _verify_resume(job: Job) -> tuple[bool, str, str]:
    """CLAUDE.md Phase 10 section 15: before ANY upload preparation, verify
    the job's own generated resume artifact actually exists and belongs to
    this job -- mirrors app.applications.executor._verify_resume_artifact's
    "job_id / artifact hash / file existence" checks (that function is
    execution-hash-comparison specific; this one is the simpler pre-session
    existence+ownership check browser_assist needs before ever opening a
    real ATS page)."""
    path_str = job.resume_pdf_path
    if not path_str:
        return False, "resume artifact not generated for this job yet -- run Prepare Application first", ""
    path = Path(path_str)
    if not path.exists():
        return False, f"resume artifact missing on disk: {path}", ""
    # Matches app.applications.executor._verify_resume_artifact's and
    # app.applications.doctor._check_wrong_resume_job_mapping's own
    # "/<job_id>/" path-segment convention -- an exact immediate-parent-name
    # match breaks the resume_optimizer's nested
    # output/<job_id>/optimized/<variant_id>/ layout (same real integration
    # gap documented in CLAUDE.md's Real Provider Execution V1 section for
    # the executor's sibling check; this function had not been updated to
    # match).
    normalized = str(path).replace("\\", "/")
    if f"/{job.id}/" not in normalized:
        return False, f"resume artifact path '{path}' does not correspond to this job", ""
    freshness = resume_integrity.verify_resume_freshness(job)
    if not freshness.fresh:
        return False, f"resume is stale: {freshness.reason}", ""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return True, "", digest


def _build_fields_for_job(job: Job, execution_id: str = "") -> list[ApplicationField]:
    """Browser-Verified Answer Canonical Readiness Integration V1: the
    generic, candidate-profile-driven ApplicationField list is UNCHANGED --
    non-stale browser-verified evidence for THIS execution (see
    app.applications.verified_field_evidence) is simply APPENDED. A
    provider-specific question with no generic mapping resolves via
    match_field_with_application_fields()'s exact-label fallback finding
    one of these appended entries; a question that already has a real
    generic alias is untouched (the generic entry stays first/authoritative
    since match_field()'s fixed alias table is always checked first)."""
    profile = load_profile()
    fields = build_application_fields(
        profile, resume_path=job.resume_pdf_path or "", cover_letter_path=job.cover_letter_path or "",
    )
    if execution_id:
        from app.applications.verified_field_evidence import build_application_field_overrides

        fields = fields + build_application_field_overrides(execution_id, job).fields
    return fields


def _classify_unresolved(raw_fields: list[dict], application_fields: list[ApplicationField],
                          unresolved_labels: list[str]) -> BrowserPauseReason:
    """CLAUDE.md Phase 10 section 13: a real legal/attestation question that
    couldn't be safely auto-filled pauses with LEGAL_ATTESTATION specifically
    (never lumped in with an ordinary unknown field) so the dashboard/user
    knows exactly what kind of review is needed."""
    labels = set(unresolved_labels)
    for rf in raw_fields:
        label = rf.get("label") or rf.get("name") or f"field#{rf.get('index')}"
        if label not in labels:
            continue
        field_id, _confidence = match_field_with_application_fields(
            rf.get("label", ""), rf.get("name", ""), application_fields,
        )
        app_field = find_field(application_fields, field_id) if field_id else None
        if app_field is not None and app_field.category == FieldCategory.LEGAL_ATTESTATION:
            return BrowserPauseReason.LEGAL_ATTESTATION
    return BrowserPauseReason.UNKNOWN_REQUIRED_FIELD


def _advance_through_apply_entry(session_id: str, outcome: "browser_runtime.DiscoveryOutcome"
                                  ) -> tuple["browser_runtime.DiscoveryOutcome", bool]:
    """CLAUDE.md Phase 11 sections 4-8: clicks through as many consecutive
    NAVIGATION_SAFE apply-entry controls as the page presents (a career
    page -> ATS landing page -> account/start page chain), re-discovering
    fresh after each click and re-validating the domain allowlist/CAPTCHA/
    login state every time (browser_runtime.rediscover already does this at
    the top of every discovery pass). Bounded so a misclassified or looping
    page can never trap this in an unbounded click loop."""
    clicked_any = False
    hops_left = _MAX_APPLY_ENTRY_HOPS
    while (
        hops_left > 0 and outcome.pause_reason is None
        and outcome.entry_detection_result == EntryDetectionResult.ENTRY_READY.value
    ):
        click_result = browser_runtime.advance_apply_entry(session_id)
        if not click_result.get("advanced"):
            break
        clicked_any = True
        outcome = browser_runtime.rediscover(session_id)
        hops_left -= 1
    return outcome, clicked_any


def _resolve_step_fields(outcome: "browser_runtime.DiscoveryOutcome", session: dict, *,
                          after_reconstruction: bool = False) -> dict:
    """CLAUDE.md Phase 11 sections 18-19: a genuinely EXACT-parsed step
    indicator on the page (e.g. "Step 2 of 4") is more authoritative than
    this module's own click-counted `current_step` -- especially after a
    reconstructed session, whose internal counter always restarts at 1 even
    though the real page may say otherwise. INFERRED/UNKNOWN confidence
    never overrides the counter, and never invents a total.

    CLAUDE.md Phase 12 sections 14-15, 26-29: also the single place every
    `_apply_discovery_outcome` return point persists iframe/shadow-DOM usage
    and checks the stage transition against the session's prior stage --
    kept here (rather than duplicated at each of the 6 return points above)
    for the same reason step-progress fields already were."""
    fields = {"step_confidence": outcome.step_confidence, "stage": outcome.stage,
              "entry_detection_result": outcome.entry_detection_result,
              "iframe_used": 1 if outcome.iframe_used else 0, "shadow_dom_used": 1 if outcome.shadow_dom_used else 0}
    # CLAUDE.md Phase 11 section 19 / Phase 12: `total_steps_if_known` is a
    # KNOWN total, and may ONLY ever be persisted from a genuinely parsed
    # (EXACT) "Step N of M" reading. `outcome.total_steps_hint` (2 if a
    # Next/Continue control is visible, else 1) is a HINT, not a reading --
    # it says "there is probably at least one more step", which is not the
    # same claim at all.
    #
    # Real Provider Execution V1 fixed a real bug here: both branches below
    # previously fell back to `total_steps_hint`, so EVERY ordinary
    # single-page session persisted `total_steps_if_known=1` alongside
    # `step_confidence='UNKNOWN'` -- precisely the invented total the rule
    # forbids, and precisely what `app.applications.doctor.
    # _check_invalid_step_progress`'s `invented_total_steps` check exists to
    # catch (it fired on every real single-page session, which this
    # feature's own Greenhouse/Lever E2E run surfaced). A previously-known
    # genuine total is still carried forward; a guess is never introduced.
    carried_total = session.get("total_steps_if_known")
    if outcome.step_confidence == StepConfidence.EXACT.value and outcome.current_step_observed:
        fields["current_step"] = outcome.current_step_observed
        fields["total_steps_if_known"] = outcome.total_steps_observed or carried_total
    else:
        fields["total_steps_if_known"] = carried_total

    if not outcome.pause_reason and session.get("stage"):
        try:
            old_stage = EntryStage(session["stage"])
            new_stage = EntryStage(outcome.stage)
        except ValueError:
            old_stage = new_stage = None
        if old_stage is not None and new_stage is not None \
                and not is_valid_stage_transition(old_stage, new_stage, after_reconstruction=after_reconstruction):
            spa_events.record(
                spa_events.EVENT_STAGE_TRANSITION_INVALID, session_id=session.get("session_id", ""),
                provider=session.get("provider", ""), stage=outcome.stage,
                detail=f"{old_stage.value} -> {new_stage.value}",
            )
    return fields


def _record_checkpoint_for_session(session: dict) -> None:
    """CLAUDE.md Phase 13 sections 37-39: best-effort checkpoint logging
    derived from the resulting session row -- never blocks, never itself
    performs recovery (see app.applications.checkpoints module docstring).
    Approximate by design: this is an audit trail of meaningful reversible
    stages reached, not a strict one-checkpoint-per-status-transition
    machine."""
    status = session.get("status", "")
    kwargs = {"job_id": session.get("job_id"), "execution_id": session.get("execution_id", "")}
    if status.startswith("PAUSED_") or status in (
        "AWAITING_USER_SUBMIT", "SUBMISSION_STATUS_UNKNOWN", "DUPLICATE_APPLICATION_DETECTED",
    ):
        checkpoints.record_checkpoint(session["session_id"], checkpoints.CheckpointStage.USER_ACTION_REQUIRED,
                                       detail=status, **kwargs)
        return
    if status == BrowserSessionStatus.READY_FOR_FINAL_SUBMIT.value:
        checkpoints.record_checkpoint(session["session_id"], checkpoints.CheckpointStage.READY_FOR_FINAL_SUBMIT,
                                       **kwargs)
        return
    mapped = session.get("mapped_field_count") or 0
    unresolved = session.get("unresolved_field_count") or 0
    if mapped or unresolved:
        checkpoints.record_checkpoint(session["session_id"], checkpoints.CheckpointStage.FORM_DISCOVERED, **kwargs)
        if mapped and not unresolved:
            checkpoints.record_checkpoint(session["session_id"], checkpoints.CheckpointStage.FIELDS_PREPARED,
                                           **kwargs)
        return
    if session.get("apply_entry_clicked") or session.get("stage") in (
        EntryStage.LANDING_PAGE.value, EntryStage.APPLICATION_ENTRY.value,
    ):
        checkpoints.record_checkpoint(session["session_id"], checkpoints.CheckpointStage.ENTRY_REACHED, **kwargs)


_DOCUMENT_KIND_BY_FIELD_ID = {
    "resume_file": document_binding.DocumentKind.RESUME,
    "cover_letter_file": document_binding.DocumentKind.COVER_LETTER,
}


def _record_document_bindings(session: dict, fill_result: "browser_runtime.FillOutcome", *,
                               form_fingerprint: str = "") -> list[dict]:
    """Real Provider Execution V1 (the brief's DOCUMENT UPLOAD requirement):
    every file that a REAL form field actually accepted gets a durable,
    append-only binding row proving WHICH artifact went to WHICH provider
    field for WHICH job.

    The artifact is re-verified against the job here
    (`document_binding.verify_artifact_matches_job`) rather than trusted:
    that is the "never silently substitute another resume" guard, and a
    failing check is recorded as an UNVERIFIED binding (with the reason)
    instead of being quietly dropped -- an audit log that omits the
    suspicious case would be worse than useless. Best-effort throughout: a
    binding-log failure must never break a real browser session, matching
    `checkpoints`/`spa_events`' own contract."""
    recorded: list[dict] = []
    job_id = session.get("job_id")
    details = getattr(fill_result, "upload_details", []) or []
    if not job_id or not details:
        return recorded
    # Only read the job when there is genuinely something to bind, so an
    # ordinary no-upload discovery pass costs nothing extra.
    job = get_job(job_id)
    resume_variant_id = (job.promoted_resume_variant_id or "") if job is not None else ""
    for detail in details:
        kind = _DOCUMENT_KIND_BY_FIELD_ID.get(detail.get("canonical_field_id") or "")
        if kind is None:
            # A file field that isn't one of our own generated documents
            # (e.g. a portfolio the candidate must attach themselves) -- we
            # never uploaded one of OUR artifacts to it, so there is nothing
            # to bind.
            continue
        artifact_path = detail.get("artifact_path") or ""
        check = document_binding.verify_artifact_matches_job(job_id, artifact_path)
        row = document_binding.record_binding_safe(
            job_id=job_id, document_kind=kind, artifact_path=artifact_path,
            provider=session.get("provider") or "", execution_id=session.get("execution_id") or "",
            session_id=session.get("session_id") or "", provider_field_id=detail.get("provider_field_id") or "",
            provider_field_label=detail.get("provider_field_label") or "", resume_variant_id=resume_variant_id,
            checkpoint=f"browser_assist:form_fingerprint={form_fingerprint[:16]}" if form_fingerprint
            else "browser_assist",
            verified=check.ok, artifact_sha256=check.sha256,
            detail=check.reason or "uploaded to the live form field and verified as this job's own artifact",
        )
        if row is not None:
            recorded.append(row)
    return recorded


def _apply_discovery_outcome(session: dict, outcome: "browser_runtime.DiscoveryOutcome",
                              application_fields: list[ApplicationField], *, check_drift: bool = True,
                              after_reconstruction: bool = False) -> dict:
    updated = _apply_discovery_outcome_raw(
        session, outcome, application_fields, check_drift=check_drift, after_reconstruction=after_reconstruction,
    )
    if updated:
        _record_checkpoint_for_session(updated)
    return updated


def _apply_discovery_outcome_raw(session: dict, outcome: "browser_runtime.DiscoveryOutcome",
                                  application_fields: list[ApplicationField], *, check_drift: bool = True,
                                  after_reconstruction: bool = False) -> dict:
    """The single place that turns one real-browser discovery pass into a
    session status update -- used by start/resume/mark-user-action-complete/
    advance-step so all four go through identical, never-diverging logic.

    `check_drift=False` is used ONLY by advance_step(): moving to a new page
    of a genuinely multi-step form is EXPECTED to have a different field
    fingerprint (different fields entirely) -- that is normal progression,
    not the "form changed out from under us" drift PAUSED_FORM_CHANGED
    exists to catch (a real E2E test against live Chromium caught this: an
    earlier version paused after every single intentional step advance,
    which would have made multi-step forms unusable)."""
    session_id = session["session_id"]

    if outcome.pause_reason:
        reason = BrowserPauseReason(outcome.pause_reason)
        status = REASON_TO_STATUS[reason]
        return browser_session.update_session(
            session_id, status=status.value, needs_user_action=1, user_action_reason=reason.value,
            **_resolve_step_fields(outcome, session, after_reconstruction=after_reconstruction),
        )

    # CLAUDE.md Phase 11 sections 4-8: pass the landing/apply-entry stage
    # before ever attempting to discover/fill a form.
    outcome, clicked_any = _advance_through_apply_entry(session_id, outcome)
    if clicked_any:
        session = browser_session.update_session(session_id, apply_entry_clicked=1)

    if outcome.pause_reason:
        reason = BrowserPauseReason(outcome.pause_reason)
        status = REASON_TO_STATUS[reason]
        return browser_session.update_session(
            session_id, status=status.value, needs_user_action=1, user_action_reason=reason.value,
            **_resolve_step_fields(outcome, session, after_reconstruction=after_reconstruction),
        )

    entry_result = outcome.entry_detection_result
    # This gate is scoped to the genuinely pre-form stages (LANDING_PAGE:
    # an apply-entry control was found but not safely followed;
    # APPLICATION_ENTRY: nothing recognized at all) -- a FINAL_REVIEW or
    # CONFIRMATION page legitimately has zero fillable fields (just a
    # submit control / success text) and must fall through to the normal
    # fill/submit-detection path below, not get treated as "unsupported".
    # `not outcome.fields` (mirrors app.applications.apply_entry.
    # detect_entry_result's own priority: real form fields always win) is
    # checked directly rather than trusting `entry_result` alone, so a
    # DiscoveryOutcome built without an explicit entry_detection_result
    # (every pre-Phase-11 call site/test that constructs one directly,
    # which the dataclass defaults to APPLICATION_ENTRY/UNSUPPORTED) is
    # never mistaken for "no form" when fields are actually present.
    _PRE_FORM_STAGES = (EntryStage.LANDING_PAGE.value, EntryStage.APPLICATION_ENTRY.value)
    if outcome.stage in _PRE_FORM_STAGES and not outcome.fields \
            and entry_result != EntryDetectionResult.FORM_ALREADY_VISIBLE.value:
        # CLAUDE.md Phase 11 section 31: no real form to fill yet, and no
        # further safe click was available -- surface exactly why instead of
        # silently reporting an empty "0 fields, ACTIVE" session.
        pause_by_result = {
            EntryDetectionResult.LOGIN_REQUIRED.value: BrowserPauseReason.LOGIN_REQUIRED,
            EntryDetectionResult.REDIRECT_REQUIRED.value: BrowserPauseReason.PLATFORM_POLICY_RESTRICTED,
            EntryDetectionResult.USER_ACTION_REQUIRED.value: BrowserPauseReason.APPLY_ENTRY_UNRECOGNIZED,
            EntryDetectionResult.UNSUPPORTED.value: BrowserPauseReason.UNSUPPORTED_SUBMISSION,
        }
        pause = pause_by_result.get(entry_result, BrowserPauseReason.UNSUPPORTED_SUBMISSION)
        status = REASON_TO_STATUS[pause]
        return browser_session.update_session(
            session_id, status=status.value, needs_user_action=1, user_action_reason=pause.value,
            **_resolve_step_fields(outcome, session, after_reconstruction=after_reconstruction),
        )

    fill_result = browser_runtime.fill_fields(session_id, outcome.fields, application_fields)
    _record_document_bindings(session, fill_result, form_fingerprint=outcome.fingerprint)

    prior_fingerprint = session.get("form_fingerprint") or ""
    form_changed = check_drift and bool(prior_fingerprint) and prior_fingerprint != outcome.fingerprint
    if form_changed:
        # CLAUDE.md Phase 10 section 33: never reuse a stale mapping blindly
        # -- surface the drift for explicit remap rather than silently
        # continuing to fill against a form that has since changed shape.
        return browser_session.update_session(
            session_id, status=BrowserSessionStatus.PAUSED_FORM_CHANGED.value, needs_user_action=1,
            user_action_reason=BrowserPauseReason.FORM_CHANGED.value, form_fingerprint=outcome.fingerprint,
            mapped_field_count=len(fill_result.filled), unresolved_field_count=len(fill_result.unresolved),
            **_resolve_step_fields(outcome, session, after_reconstruction=after_reconstruction),
        )

    if fill_result.unresolved:
        reason = _classify_unresolved(outcome.fields, application_fields, fill_result.unresolved)
        status = REASON_TO_STATUS[reason]
        return browser_session.update_session(
            session_id, status=status.value, needs_user_action=1, user_action_reason=reason.value,
            form_fingerprint=outcome.fingerprint, mapped_field_count=len(fill_result.filled),
            unresolved_field_count=len(fill_result.unresolved),
            **_resolve_step_fields(outcome, session, after_reconstruction=after_reconstruction),
        )

    # CLAUDE.md Phase 10 section 29: the runtime may LOCATE a submit button,
    # never click it -- READY_FOR_FINAL_SUBMIT just means "report it, and
    # stop here" for every provider in this project (none have
    # AutomationPolicy.PERMITTED_AUTO for a real browser session today).
    new_status = BrowserSessionStatus.READY_FOR_FINAL_SUBMIT if outcome.submit_button else BrowserSessionStatus.ACTIVE
    return browser_session.update_session(
        session_id, status=new_status.value, needs_user_action=0, user_action_reason="",
        form_fingerprint=outcome.fingerprint, mapped_field_count=len(fill_result.filled),
        unresolved_field_count=len(fill_result.unresolved),
        **_resolve_step_fields(outcome, session, after_reconstruction=after_reconstruction),
    )


def start_session(execution_id: str) -> dict:
    """Opens a real, visible browser against the execution's job's actual
    application URL. Re-derives eligibility independently (CLAUDE.md Phase 10
    sections 1-2, acceptance scenarios B/C) -- a hard-skip or not-yet-eligible
    job NEVER gets a browser session, full stop, regardless of what called
    this. Idempotent: an existing active session for the same job is reused
    rather than creating a duplicate (section 49)."""
    execution = _executions_repo.get_execution(execution_id)
    if execution is None:
        return {"created": False, "reason": f"execution {execution_id} not found"}
    job = get_job(execution["job_id"])
    if job is None:
        return {"created": False, "reason": f"job {execution['job_id']} not found"}

    eligibility = evaluate_executor_eligibility(job)
    if not eligibility.enters_queue:
        return {
            "created": False,
            "reason": "; ".join(eligibility.reasons) or "job is not eligible for application preparation",
            "hard_skip": eligibility.hard_skip,
        }

    existing = browser_session.get_active_session_for_job(job.id)
    if existing is not None:
        # CLAUDE.md Phase 11 section 63: delegate to resume_session() rather
        # than duplicating its claim/reopen/reconstruct logic here -- it
        # already handles "session owned by another worker", "still live in
        # this process", and "process gone, reconstruct" uniformly.
        result = resume_session(existing["session_id"])
        return {"created": True, "session": result.get("session", existing),
                "reason": result.get("detail", "existing active session reused")}

    # CLAUDE.md Phase 12 sections 25-27: prefer a provider-resolved direct
    # application URL over the more generic job-detail URL -- avoids an
    # unnecessary landing-page apply-entry hop when the discovery-time
    # provider adapter already resolved straight to the real form (Lever/
    # Ashby's real postings both had this shape in Phase 11's own findings).
    resolved = resolve_application_url(
        canonical_url=job.canonical_url or "", job_url=job.url or "", provider=job.provider or "",
    )
    application_url = resolved.url
    if not application_url:
        return {"created": False, "reason": "no application URL available for this job"}

    resume_ok, resume_reason, resume_hash = _verify_resume(job)
    if not resume_ok:
        return {"created": False, "reason": resume_reason}

    application_fields = _build_fields_for_job(job, execution_id)
    answers_version = sum(1 for f in application_fields if f.verified_value is not None)

    try:
        session = browser_session.create_session(
            execution_id=execution_id, job_id=job.id, provider=job.provider or "", application_url=application_url,
        )
    except DuplicateSessionError:
        existing = browser_session.get_active_session_for_job(job.id)
        if existing is None:
            return {"created": False, "reason": f"job {job.id} already has an active session that could not be found"}
        result = resume_session(existing["session_id"])
        return {"created": True, "session": result.get("session", existing),
                "reason": result.get("detail", "race: session already existed")}

    session_id = session["session_id"]
    session = browser_session.update_session(
        session_id, resume_artifact_hash=resume_hash, answers_version=answers_version,
        url_provenance=resolved.provenance.value,
    )

    owned, session = _claim_or_conflict(session_id)
    if not owned:
        return {"created": True, "session": session,
                "reason": "session created but is already owned by another worker/process"}
    try:
        try:
            outcome = browser_runtime.open_session(
                session_id, provider=job.provider or "", url=application_url, job_id=job.id,
                expected_title=job.title or "", expected_company=job.company or "",
                expected_location=job.location or "",
            )
        except (browser_runtime.BrowserRuntimeUnavailable, browser_runtime.BrowserRuntimeBusy) as exc:
            updated = browser_session.update_session(
                session_id, status=BrowserSessionStatus.CLOSED.value, user_action_reason=str(exc),
            )
            return {"created": False, "reason": str(exc), "session": updated}

        updated = _apply_discovery_outcome(session, outcome, application_fields)
        return {"created": True, "session": updated}
    finally:
        browser_session.release_session_lease(session_id)


def resume_session(session_id: str) -> dict:
    """CLAUDE.md Phase 10 section 6/51: resumes an existing session. If the
    browser is still live in THIS process, re-scans the CURRENT page state
    (never assumes it stayed unchanged) and continues. If the browser/process
    is gone, restarts a fresh browser at the SAME URL when that's still safe
    (pre-submission) -- or, if the session was last known to be awaiting a
    manual submit, honestly marks SUBMISSION_STATUS_UNKNOWN rather than
    guessing whether the submission went through."""
    session = browser_session.get_session(session_id)
    if session is None:
        return {"ok": False, "detail": f"session {session_id} not found"}
    if session["status"] in _TERMINAL_SESSION_VALUES:
        return {"ok": True, "detail": f"session already {session['status']}", "session": session}

    owned, current = _claim_or_conflict(session_id)
    if not owned:
        return {"ok": False, "detail": "session is currently owned by another worker/process", "session": current}
    session = current

    try:
        job = get_job(session["job_id"])
        if job is None:
            return {"ok": False, "detail": f"job {session['job_id']} not found"}
        application_fields = _build_fields_for_job(job, session.get("execution_id", ""))

        if browser_runtime.is_live(session_id):
            browser_session.touch_activity(session_id)
            outcome = browser_runtime.rediscover(session_id)
            updated = _apply_discovery_outcome(session, outcome, application_fields)
            return {"ok": True, "detail": "resumed live browser session", "session": updated}

        if session["status"] == BrowserSessionStatus.AWAITING_USER_SUBMIT.value:
            updated = browser_session.update_session(
                session_id, status=BrowserSessionStatus.SUBMISSION_STATUS_UNKNOWN.value, needs_user_action=1,
                user_action_reason="the browser process was lost while awaiting a manual submit -- outcome unknown, "
                                    "reconciliation required",
            )
            return {
                "ok": False,
                "detail": "browser was lost while awaiting a manual submit -- marked SUBMISSION_STATUS_UNKNOWN",
                "session": updated,
            }

        if not config.BROWSER_SESSION_RECONSTRUCT_ENABLED:
            return {"ok": False, "session": session,
                    "detail": "browser process was lost and BROWSER_SESSION_RECONSTRUCT_ENABLED is false -- an "
                              "explicit human restart is required"}

        try:
            outcome = browser_runtime.open_session(
                session_id, provider=session["provider"], url=session["application_url"], job_id=job.id,
                expected_title=job.title or "", expected_company=job.company or "",
                expected_location=job.location or "",
            )
        except (browser_runtime.BrowserRuntimeUnavailable, browser_runtime.BrowserRuntimeBusy) as exc:
            return {"ok": False, "detail": str(exc), "session": session}

        # CLAUDE.md Phase 11 section 25: this is honestly a RECONSTRUCTION
        # (fresh browser, the session's saved application_url, full
        # rediscovery) -- never a claim of true cross-process browser
        # reattachment. `reconstructed_count` tracks how many times this has
        # happened for dashboard/metrics visibility.
        session = browser_session.update_session(
            session_id, reconstructed_count=(session.get("reconstructed_count") or 0) + 1,
        )
        updated = _apply_discovery_outcome(session, outcome, application_fields, after_reconstruction=True)
        return {"ok": True, "session": updated,
                "detail": "reconstructed a fresh browser window at the saved application URL (previous "
                          "window/process was gone) -- the form was rediscovered from scratch, not reattached"}
    finally:
        browser_session.release_session_lease(session_id)


def mark_user_action_complete(session_id: str) -> dict:
    """CLAUDE.md Phase 10 section 8: the candidate says they finished the
    required action (logged in, solved the CAPTCHA, decided the legal
    question in their own head, etc) in the visible window -- rediscovers the
    CURRENT form state and continues, exactly like resume_session, but only
    valid from a PAUSED_* status."""
    session = browser_session.get_session(session_id)
    if session is None:
        return {"ok": False, "detail": f"session {session_id} not found"}
    if session["status"] not in _PAUSED_VALUES:
        return {"ok": False, "detail": f"session is {session['status']}, not waiting on a user action",
                "session": session}
    return resume_session(session_id)


def record_verified_custom_answer(session_id: str, question_label_prefix: str, answer_value: str) -> dict:
    """Browser-Verified Answer Canonical Readiness Integration V1: the ONLY
    sanctioned way an explicit, human-provided answer to a provider-
    specific question (one with no generic candidate-profile mapping) ever
    becomes durable, canonical evidence. Resolves the field matching
    `question_label_prefix` on the session's CURRENT live page (by
    normalized-label prefix -- never positional), fills it via the SAME
    type-aware `browser_runtime.fill_one_field()` dispatch every other fill
    path uses (including `_fill_combobox`'s scoped-listbox resolution and
    displayed-value verification for a combobox), and ONLY IF that fill
    genuinely succeeded records a `browser_verified_field_evidence` row --
    a click/fill that returned False, or a field that could not be found
    at all, records NOTHING (see CLAUDE.md Reliable Form Interaction V1:
    "never claim success from a mere click/fill not throwing").

    Returns {"ok": bool, "detail": str, "expected": str, "actual": str|None,
    "evidence_id": str|None}."""
    session = browser_session.get_session(session_id)
    if session is None:
        return {"ok": False, "detail": f"session {session_id} not found", "expected": answer_value,
                "actual": None, "evidence_id": None}
    if not browser_runtime.is_live(session_id):
        return {"ok": False, "detail": "browser session is not open in this process -- resume it first",
                "expected": answer_value, "actual": None, "evidence_id": None}

    job = get_job(session["job_id"])
    if job is None:
        return {"ok": False, "detail": f"job {session['job_id']} not found", "expected": answer_value,
                "actual": None, "evidence_id": None}

    outcome = browser_runtime.rediscover(session_id)
    if outcome.pause_reason:
        return {"ok": False, "detail": f"a genuine pause ({outcome.pause_reason}) is present -- not recording "
                                        f"anything against an unstable page state",
                "expected": answer_value, "actual": None, "evidence_id": None}

    prefix_n = _norm_label_for_matching(question_label_prefix)
    rf = next(
        (f for f in outcome.fields if _norm_label_for_matching(f.get("label", "")).startswith(prefix_n)),
        None,
    )
    if rf is None:
        return {"ok": False, "detail": f"no field on the current page matches label prefix {question_label_prefix!r}",
                "expected": answer_value, "actual": None, "evidence_id": None}

    ok = browser_runtime.fill_one_field(session_id, rf, answer_value)
    if not ok:
        return {"ok": False, "detail": "fill/verification did not succeed -- recording nothing",
                "expected": answer_value, "actual": None, "evidence_id": None}

    actual = browser_runtime.read_displayed_value(session_id, rf)
    if not actual:
        return {"ok": False, "detail": "fill reported success but no displayed value could be read afterward -- "
                                        "recording nothing (silence is never treated as proof)",
                "expected": answer_value, "actual": None, "evidence_id": None}

    if rf.get("type") in ("checkbox", "radio"):
        # A checkbox/radio's live displayed value is boolean
        # (`_read_displayed_value`'s `is_checked()` -> "true"/"false"), not
        # text comparable to the answer text used only to LOCATE/click it
        # via `get_by_label` -- e.g. a single self-labeled opt-in consent
        # checkbox, where the "answer" IS the click target's own label.
        # Verification here means exactly "did it end up checked" -- never
        # loosened to accept "false" (this path never unchecks anything;
        # `_fill_one`'s checkbox/radio branch only ever calls `.check()`).
        if actual != "true":
            return {"ok": False, "detail": f"checkbox/radio did not end up checked (actual={actual!r}) -- "
                                            f"recording nothing",
                    "expected": answer_value, "actual": actual, "evidence_id": None}
    else:
        # Never record evidence whose actual displayed value doesn't
        # genuinely correspond to the intended answer -- `_fill_one`
        # reporting `ok=True` for a plain text field only means `.fill()`
        # didn't raise, not that the right field received it (a real live
        # bug: a nearby field's value was misread here before this check
        # existed). Bidirectional substring match mirrors
        # `_fill_combobox`'s own verification; empty strings are excluded
        # from both sides so this can never trivially "match" nothing.
        expected_norm = answer_value.strip().lower()
        actual_norm = actual.strip().lower()
        if not (expected_norm and actual_norm and (expected_norm in actual_norm or actual_norm in expected_norm)):
            return {"ok": False, "detail": f"displayed value {actual!r} does not correspond to the intended answer "
                                            f"{answer_value!r} -- recording nothing",
                    "expected": answer_value, "actual": actual, "evidence_id": None}

    from app.applications.verified_field_evidence import record_verified_answer

    evidence_id = record_verified_answer(
        execution_id=session.get("execution_id", ""), job_id=job.id, provider=job.provider or "",
        session_id=session_id, question_label=rf.get("label", question_label_prefix),
        field_type=rf.get("type", ""), required=bool(rf.get("required")),
        expected_answer=answer_value, actual_displayed_value=actual,
        structural_form_fingerprint=outcome.fingerprint, job=job,
    )
    return {"ok": True, "detail": "verified and recorded", "expected": answer_value, "actual": actual,
            "evidence_id": evidence_id}


def _norm_label_for_matching(label: str) -> str:
    return " ".join((label or "").split())


def advance_step(session_id: str) -> dict:
    """CLAUDE.md Phase 10 section 10: clicks a safe "Next"/"Continue" control
    on a multi-step form (never a final submit action) and rediscovers the
    resulting page. Requires a live browser -- this is not something a
    restarted process can safely replay, since intermediate steps may depend
    on a previous page's now-cleared in-memory state."""
    session = browser_session.get_session(session_id)
    if session is None:
        return {"ok": False, "detail": f"session {session_id} not found"}
    if not browser_runtime.is_live(session_id):
        return {"ok": False, "detail": "browser session is not open in this process -- resume it first",
                "session": session}

    owned, current = _claim_or_conflict(session_id)
    if not owned:
        return {"ok": False, "detail": "session is currently owned by another worker/process", "session": current}
    session = current
    try:
        result = browser_runtime.advance_step(session_id)
        if not result.get("advanced"):
            reason = result.get("reason", "could not advance to the next step")
            if result.get("validation_errors"):
                reason = f"{reason}: {'; '.join(result['validation_errors'][:3])}"
            return {"ok": False, "detail": reason, "session": session}

        job = get_job(session["job_id"])
        application_fields = _build_fields_for_job(job, session.get("execution_id", "")) if job else []
        session = browser_session.update_session(session_id, current_step=result["current_step"])
        checkpoints.record_checkpoint(
            session_id, checkpoints.CheckpointStage.STEP_COMPLETED, job_id=session.get("job_id"),
            execution_id=session.get("execution_id", ""), detail=f"step {result['current_step']}",
        )
        outcome = browser_runtime.rediscover(session_id)
        updated = _apply_discovery_outcome(session, outcome, application_fields, check_drift=False)
        return {"ok": True, "detail": "advanced to the next step", "session": updated}
    finally:
        browser_session.release_session_lease(session_id)


def pause_session(session_id: str, reason: BrowserPauseReason) -> dict:
    status = REASON_TO_STATUS.get(reason, BrowserSessionStatus.PAUSED_UNKNOWN_FIELD)
    return browser_session.update_session(
        session_id, status=status.value, needs_user_action=1, user_action_reason=reason.value,
    )


def close_session(session_id: str, *, reason: str = "closed by user") -> dict:
    """CLAUDE.md Phase 10 section 6: always safe to call, even if the browser
    was already gone -- never raises, never corrupts execution state."""
    browser_runtime.close_session(session_id)
    return browser_session.close_session(session_id, reason=reason)


def expire_stale_sessions() -> list[dict]:
    """CLAUDE.md Phase 10 section 50: reaps abandoned sessions -- never
    auto-submits or deletes evidence, only flips status/frees the (bounded)
    browser concurrency slot."""
    expired = browser_session.expire_stale_sessions(timeout_minutes=config.BROWSER_SESSION_TIMEOUT_MINUTES)
    for row in expired:
        browser_runtime.close_session(row["session_id"])
    return expired


def _record_receipt_best_effort(**kwargs) -> None:
    """Mirrors app.applications.executor._record_receipt_best_effort -- a
    receipt-recording failure must never turn an already-genuinely-confirmed
    APPLIED execution into an error."""
    try:
        latest_approval = _approval.get_latest_approval(kwargs["execution_id"])
        approval_id = latest_approval["approval_id"] if latest_approval else ""
        _receipts.record_receipt(approval_id=approval_id, **kwargs)
    except Exception:  # noqa: BLE001
        pass


def attempt_user_submit_reconciliation(session_id: str) -> dict:
    """CLAUDE.md Phase 10 sections 40-42: the candidate says they clicked the
    real submit button themselves in the visible window. Inspects the
    CURRENT page for genuine confirmation evidence (never fabricated) -- only
    a positively observed success indicator marks the linked execution
    APPLIED; anything else leaves the session/execution exactly as they
    were, or (if the browser is no longer reachable at all) honestly
    SUBMISSION_STATUS_UNKNOWN."""
    session = browser_session.get_session(session_id)
    if session is None:
        return {"ok": False, "detail": f"session {session_id} not found"}
    if session["status"] == BrowserSessionStatus.CONFIRMED.value:
        return {"ok": True, "detail": "already confirmed", "session": session}

    owned, current = _claim_or_conflict(session_id)
    if not owned:
        return {"ok": False, "detail": "session is currently owned by another worker/process", "session": current}
    session = current
    try:
        if not browser_runtime.is_live(session_id):
            updated = browser_session.update_session(
                session_id, status=BrowserSessionStatus.SUBMISSION_STATUS_UNKNOWN.value, needs_user_action=1,
                user_action_reason="browser window is not reachable in this process -- cannot verify whether the "
                                    "manual submit succeeded",
            )
            return {"ok": False, "detail": "browser not reachable -- outcome unknown", "session": updated}

        outcome = browser_runtime.capture_confirmation(session_id)

        if outcome.already_applied:
            # CLAUDE.md Phase 11 section 36: "you already applied" is
            # evidence of a PRIOR application -- never folded into a fresh
            # CONFIRMED/APPLIED event; always left for a human to reconcile.
            execution = _executions_repo.get_execution(session["execution_id"])
            if execution is not None and execution["active"] == 1:
                _executions_repo.log_event(
                    execution["execution_id"], execution["job_id"], "duplicate_detected",
                    detail="browser_assist_already_applied_evidence",
                )
            updated = browser_session.update_session(
                session_id, status=BrowserSessionStatus.DUPLICATE_APPLICATION_DETECTED.value, needs_user_action=1,
                user_action_reason="the page indicates an application already exists for this job -- a human must "
                                    "reconcile whether this is the same application or a genuine duplicate",
            )
            return {"ok": False, "detail": "page shows an existing/duplicate application -- reconciliation required",
                    "session": updated}

        if not outcome.confirmed:
            browser_session.touch_activity(session_id)
            return {"ok": False, "detail": "no confirmation evidence found on the current page yet",
                    "session": session}

        execution = _executions_repo.get_execution(session["execution_id"])
        if execution is not None and execution["active"] == 1:
            _executions_repo.update_execution(
                execution["execution_id"], execution["job_id"], ExecutionStatus.APPLIED,
                confirmation_id=outcome.confirmation_id, confirmation_url=outcome.current_url,
                confirmation_text_fingerprint=outcome.confirmation_text_fingerprint,
                user_action_reason="confirmed via browser-assist manual submission", requires_user_action=0,
            )
            _executions_repo.log_event(
                execution["execution_id"], execution["job_id"], "confirmed", detail="browser_assist_manual_submit",
            )
            _record_receipt_best_effort(
                execution_id=execution["execution_id"], job_id=execution["job_id"], provider=session.get("provider", ""),
                submitted_via=f"browser_assist:{session.get('provider', '')}", confirmation_id=outcome.confirmation_id,
                sanitized_url=outcome.current_url, evidence_strength=outcome.evidence_strength,
                raw_message_fingerprint=outcome.confirmation_text_fingerprint, session_id=session_id,
            )

        updated = browser_session.update_session(
            session_id, status=BrowserSessionStatus.CONFIRMED.value, confirmation_observed=1,
            confirmation_id=outcome.confirmation_id, confirmation_url=outcome.current_url,
            confirmation_text_fingerprint=outcome.confirmation_text_fingerprint, needs_user_action=0,
            confirmation_evidence_strength=outcome.evidence_strength,
        )
        browser_runtime.close_session(session_id)
        return {"ok": True, "detail": "confirmed", "session": updated}
    finally:
        browser_session.release_session_lease(session_id)


def attempt_user_submit_reconciliation_from_evidence(
    session_id: str, *, current_url: str, body_text: str,
) -> dict:
    """Same decision (CONFIRMED / duplicate-detected / no-evidence-yet) and
    the SAME persistence as attempt_user_submit_reconciliation() -- reusing
    the identical confirmation_parser/confirmation_evidence pipeline every
    other confirmation path in this project goes through -- but driven by
    INDEPENDENTLY obtained page evidence rather than a live browser DOM read.

    Exists for exactly one situation: a live browser session cannot be
    safely re-inspected in-process (this project's per-process browser
    registry means a diagnostic call from the wrong thread/process can fail
    or, worse, force a reconstruction that reloads the session's ORIGINAL
    application_url and loses whatever page was actually showing -- a real
    problem hit live, 2026-08-31, verifying job 200/Robinhood's post-submit
    Greenhouse confirmation page), while the operator has independently
    obtained genuine page content through some other safe channel (here: a
    plain HTTP GET of the provider's own public, static, session-independent
    confirmation URL -- Greenhouse's `/confirmation` route requires no
    candidate-specific auth/cookie to render, verified by fetching it with a
    fresh, cookie-less client and getting the same content the live browser
    had navigated to).

    NEVER accepts a bare claim of success -- `body_text` still goes through
    the exact same phrase-match + evidence-grading pipeline; only a
    STRONG/MODERATE-graded match confirms. Never touches any live browser
    (no is_live() check, no browser_runtime.close_session() call) -- the
    caller is responsible for the evidence's provenance and genuineness;
    this function is responsible only for applying this project's existing,
    unmodified confirmation-grading and persistence rules to it."""
    from app.applications.confirmation_evidence import classify_confirmation_evidence
    from app.applications.confirmation_parser import parse_confirmation_text

    session = browser_session.get_session(session_id)
    if session is None:
        return {"ok": False, "detail": f"session {session_id} not found"}
    if session["status"] == BrowserSessionStatus.CONFIRMED.value:
        return {"ok": True, "detail": "already confirmed", "session": session}

    owned, current = _claim_or_conflict(session_id)
    if not owned:
        return {"ok": False, "detail": "session is currently owned by another worker/process", "session": current}
    session = current
    try:
        parsed = parse_confirmation_text(body_text)

        if parsed.already_applied:
            execution = _executions_repo.get_execution(session["execution_id"])
            if execution is not None and execution["active"] == 1:
                _executions_repo.log_event(
                    execution["execution_id"], execution["job_id"], "duplicate_detected",
                    detail="browser_assist_already_applied_evidence_external",
                )
            updated = browser_session.update_session(
                session_id, status=BrowserSessionStatus.DUPLICATE_APPLICATION_DETECTED.value, needs_user_action=1,
                user_action_reason="the supplied evidence indicates an application already exists for this job -- "
                                    "a human must reconcile whether this is the same application or a genuine "
                                    "duplicate",
            )
            return {"ok": False, "detail": "supplied evidence shows an existing/duplicate application -- "
                                            "reconciliation required", "session": updated}

        grade = classify_confirmation_evidence(
            phrase_matched=parsed.phrase_matched, confirmation_id=parsed.confirmation_id, current_url=current_url,
        )
        if not grade.confirms():
            return {"ok": False, "detail": "no confirmation evidence found in the supplied evidence",
                    "session": session}

        execution = _executions_repo.get_execution(session["execution_id"])
        if execution is not None and execution["active"] == 1:
            _executions_repo.update_execution(
                execution["execution_id"], execution["job_id"], ExecutionStatus.APPLIED,
                confirmation_id=parsed.confirmation_id, confirmation_url=current_url,
                confirmation_text_fingerprint=parsed.text_fingerprint,
                user_action_reason="confirmed via independently-obtained post-submit evidence",
                requires_user_action=0,
            )
            _executions_repo.log_event(
                execution["execution_id"], execution["job_id"], "confirmed",
                detail="browser_assist_external_evidence_reconciliation",
            )
            _record_receipt_best_effort(
                execution_id=execution["execution_id"], job_id=execution["job_id"],
                provider=session.get("provider", ""),
                submitted_via=f"browser_assist_external_evidence:{session.get('provider', '')}",
                confirmation_id=parsed.confirmation_id, sanitized_url=current_url,
                evidence_strength=grade.strength.value, raw_message_fingerprint=parsed.text_fingerprint,
                session_id=session_id,
            )

        updated = browser_session.update_session(
            session_id, status=BrowserSessionStatus.CONFIRMED.value, confirmation_observed=1,
            confirmation_id=parsed.confirmation_id, confirmation_url=current_url,
            confirmation_text_fingerprint=parsed.text_fingerprint, needs_user_action=0,
            confirmation_evidence_strength=grade.strength.value,
        )
        return {"ok": True, "detail": "confirmed via independently-obtained evidence", "session": updated}
    finally:
        browser_session.release_session_lease(session_id)
