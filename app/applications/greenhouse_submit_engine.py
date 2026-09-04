"""Greenhouse Verified Submission Contract V1 -- the actual submit engine.

This module performs the ONE real thing every other module in
`app.applications` deliberately refuses to do: it clicks a final submit
control on a real Greenhouse-shaped application page. It is reachable ONLY
through `app.applications.greenhouse_canary` (disabled by default, requires
explicit per-job operator action) and directly from this feature's own test
suite (against local `file://` fixtures, never a real employer) -- it is
NEVER wired into `app.applications.executor.process_execution()`'s ordinary
pipeline, `app.applications.browser_assist`, or any scheduled/background
loop, so the ordinary ASSIST pipeline is completely unaffected by this
module's existence.

Reuse, not a parallel system: every step up to and including a fully filled,
fully validated, job-identity-verified page ready to submit is produced by
calling `app.applications.browser_assist.start_session()`/`resume_session()`
completely UNCHANGED -- this module adds exactly one new capability (the
final click + outcome classification) on top of that already-proven-safe
session, then hands the result to the SAME confirmation/receipt/blocker
machinery every other submission path already uses
(`app.applications.confirmation_parser`, `app.applications.confirmation_evidence`,
`app.applications.receipts`, `app.applications.blockers`).

Every submit attempt is preceded by re-checking the full
`app.applications.greenhouse_submit_contract` and by acquiring the atomic,
one-time-only `app.applications.greenhouse_submit_claim` -- a second call for
the same execution is refused before a browser is even opened. Any outcome
that cannot be confidently classified CONFIRMED or REJECTED becomes
SUBMISSION_STATUS_UNKNOWN and is never, by this module or any other, retried
automatically."""

import logging
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlparse

from app import config
from app.applications import blockers, receipts, repo
from app.applications import greenhouse_submit_claim as claim
from app.applications.apply_entry import ApplyControlClassification, classify_apply_control_detailed
from app.applications.confirmation_evidence import classify_confirmation_evidence
from app.applications.confirmation_parser import parse_confirmation_text
from app.applications.domain_allowlist import is_allowed_host_for_session
from app.applications.greenhouse_submit_contract import SubmitOutcome, build_submit_contract
from app.applications.models import ExecutionStatus
from app.jobs_repo import get_job
from app.models import Job

logger = logging.getLogger("applications.greenhouse_submit_engine")

_WORKER_ID = "greenhouse_submit_engine"


class EngineUnavailable(Exception):
    """BROWSER_ASSIST_ENABLED is false or Playwright isn't installed."""


@dataclass
class SubmitAttemptResult:
    outcome: SubmitOutcome
    error_type: str = ""
    detail: str = ""
    confirmation_id: str = ""
    confirmation_url: str = ""
    confirmation_text_fingerprint: str = ""

    def as_dict(self) -> dict:
        return {
            "outcome": self.outcome.value, "error_type": self.error_type, "detail": self.detail,
            "confirmation_id": self.confirmation_id, "confirmation_url": self.confirmation_url,
            "confirmation_text_fingerprint": self.confirmation_text_fingerprint,
        }


def _require_available() -> None:
    if not config.BROWSER_ASSIST_ENABLED:
        raise EngineUnavailable("BROWSER_ASSIST_ENABLED is false.")
    from app.applications.browser_runtime import playwright_available

    if not playwright_available():
        raise EngineUnavailable(
            "playwright is not installed -- run `pip install playwright && playwright install chromium`."
        )


def _scan_final_submit_controls(live) -> list[dict]:
    """Runs inside the live session's dedicated Playwright thread. Returns
    every VISIBLE control this page's DOM currently exposes that
    `app.applications.apply_entry` -- the single source of this
    classification, never a second parallel table -- classifies
    FINAL_SUBMIT. Reuses the exact same shadow-DOM-piercing deep-query
    helper every other DOM scan in `app.applications.browser_runtime`
    uses."""
    from app.applications.browser_runtime import _DEEP_QUERY_JS

    page = live.page
    current_host = urlparse(page.url).hostname or ""
    raw = page.evaluate(
        """
        () => {"""
        + _DEEP_QUERY_JS +
        """
          const isVisible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
          const els = __deepQueryAll(document, 'a, button, input[type=submit], input[type=button], [role="button"]');
          return els.filter(isVisible).map((el) => ({
            text: ((el.innerText || el.value || '') + '').trim(),
            href: el.tagName === 'A' ? (el.getAttribute('href') || '') : '',
            id: el.id || '',
          })).filter((c) => c.text);
        }
        """
    )
    finals = []
    for c in raw:
        detail = classify_apply_control_detailed(c["text"], href=c["href"], current_host=current_host)
        if detail.classification == ApplyControlClassification.FINAL_SUBMIT:
            finals.append(c)
    return finals


_FILLABLE_FIELDS_SELECTOR = (
    "input:not([type=hidden]):not([type=password]):not([type=submit]):not([type=button]), textarea, select"
)


def _count_fillable_fields(live) -> int:
    """Runs inside the live session's dedicated Playwright thread. Same
    selector `_wait_for_stable_state()` already uses for its own
    has_fields check -- never a second, differently-scoped query."""
    try:
        return live.page.locator(_FILLABLE_FIELDS_SELECTOR).count()
    except Exception:  # noqa: BLE001 -- a page mid-navigation may throw transiently
        return -1


def _click_and_observe(
    live, control: dict, *, click_timeout_ms: int, observe_timeout_ms: int, observe_poll_ms: int,
    pre_field_count: int = -1,
) -> dict:
    """Runs inside the live session's dedicated Playwright thread. The ONE
    physical click this entire feature exists to perform -- and the ONLY
    place in this project that ever calls `.click()` on a control classified
    FINAL_SUBMIT -- PLUS the bounded observation of what happened
    immediately afterward, kept in a single call so the network-failure
    listener registered before the click stays attached through the async
    response/failure that click can trigger (a fetch failure fires well
    after `.click()` itself returns, so a listener removed right after the
    click would silently miss it).

    Uses a genuine Playwright locator (never a JS-dispatched synthetic
    click) so a disabled/unresponsive control genuinely raises a Playwright
    TimeoutError -- the real, honest "no click was ever dispatched" signal
    `run_greenhouse_submit` depends on to distinguish a pre-click timeout
    from a post-click one. Never assumes success or failure -- only reports
    what was genuinely observed.

    `app.applications.browser_runtime._wait_for_stable_state()` is built for
    the PRE-fill discovery pass (it returns as soon as ANY fillable field is
    present, which is true immediately after a click too -- the ORIGINAL
    form is still on the page until its async response actually arrives)
    and is therefore the wrong tool for "did the page change AS A RESULT of
    the click I just made" -- so this polls for the clicked control's OWN
    disappearance instead, the precise, click-correlated signal a real
    submit flow's async response (or its total absence) produces."""
    import time

    from app.applications.browser_runtime import _detect_validation_errors

    page = live.page
    selector = f"#{control['id']}" if control.get("id") else None
    failed_requests: list[str] = []

    def _on_request_failed(request) -> None:
        try:
            failed_requests.append(request.url)
        except Exception:  # noqa: BLE001
            pass

    page.on("requestfailed", _on_request_failed)
    try:
        try:
            if selector:
                locator = page.locator(selector)
            else:
                locator = page.get_by_text(control["text"], exact=True).first
            locator.click(timeout=click_timeout_ms)
            clicked = True
            click_error = ""
        except Exception as exc:  # noqa: BLE001 -- Playwright's own TimeoutError/Error
            clicked = False
            click_error = f"{type(exc).__name__}: {exc}"[:500]

        if not clicked:
            return {"clicked": False, "click_error": click_error, "failed_requests": list(failed_requests),
                    "timed_out": False, "validation_errors": [], "body_text": "", "url": page.url,
                    "heading_text": "", "submit_control_disappeared": None, "form_fields_disappeared": None}

        changed = selector is None  # no id to track -- fall back to "assume changed", scan decides
        if selector:
            deadline = time.monotonic() + (observe_timeout_ms / 1000.0)
            poll_s = max(0.05, observe_poll_ms / 1000.0)
            while time.monotonic() < deadline:
                try:
                    still_present = page.locator(selector).count() > 0
                except Exception:  # noqa: BLE001 -- a page mid-navigation may throw transiently; keep polling
                    still_present = True
                if not still_present:
                    changed = True
                    break
                if failed_requests:
                    # The network already told us this attempt failed --
                    # never wait out the rest of the observation window.
                    break
                time.sleep(poll_s)

        validation = _detect_validation_errors(page)
        try:
            body_text = page.inner_text("body")
        except Exception:  # noqa: BLE001
            body_text = validation.get("body_text", "")
        try:
            heading_text = page.evaluate("() => (document.querySelector('h1,h2')||{}).innerText || ''")
        except Exception:  # noqa: BLE001
            heading_text = ""
        post_field_count = _count_fillable_fields(live)
        # Structural corroboration is conservative BY DESIGN (see
        # confirmation_evidence.classify_confirmation_evidence's docstring):
        # only a genuine before/after comparison (both counts actually
        # observed, never -1) where the form's fields have GENUINELY gone to
        # zero counts as "the form disappeared" -- never guessed from a
        # single-sided observation.
        form_fields_disappeared = (
            pre_field_count > 0 and post_field_count == 0
        ) if pre_field_count >= 0 and post_field_count >= 0 else None
        return {
            "clicked": True, "click_error": "", "failed_requests": list(failed_requests),
            "timed_out": not changed and not failed_requests,
            "validation_errors": validation.get("errors") or [], "body_text": body_text, "url": page.url,
            "heading_text": heading_text, "submit_control_disappeared": changed,
            "form_fields_disappeared": form_fields_disappeared,
        }
    finally:
        try:
            page.remove_listener("requestfailed", _on_request_failed)
        except Exception:  # noqa: BLE001
            pass


def run_greenhouse_submit(
    job_id: int, *, headless: bool = True, _test_route_hook: Optional[Callable] = None,
) -> SubmitAttemptResult:
    """The engine's single entry point. Never called directly by anything
    other than `app.applications.greenhouse_canary` and this feature's own
    tests -- see module docstring.

    `_test_route_hook` is a TEST-ONLY extension point (never used by
    `greenhouse_canary`): an optional callable invoked with the live
    Playwright `page` immediately after the session reaches
    READY_FOR_FINAL_SUBMIT and before the submit control is scanned/clicked,
    so a test can register `page.route(...)` interception for the fixture's
    own `fetch()` call (success/validation-error/timeout/connection-loss/
    duplicate) -- there is no other seam to do this from outside, since this
    function otherwise owns the entire browser session's lifecycle."""
    job = get_job(job_id)
    if job is None:
        return SubmitAttemptResult(SubmitOutcome.BLOCKED, "JOB_NOT_FOUND", f"job {job_id} not found")

    contract = build_submit_contract(job_id)
    if contract is None or not contract.execution_id:
        return SubmitAttemptResult(SubmitOutcome.BLOCKED, "NO_EXECUTION", "no active execution for this job")
    execution_id = contract.execution_id

    if contract.already_attempted:
        return SubmitAttemptResult(
            SubmitOutcome.BLOCKED, "ALREADY_ATTEMPTED",
            "a submit action was already attempted for this execution -- never retried",
        )
    if not contract.ready:
        return SubmitAttemptResult(
            SubmitOutcome.BLOCKED, "CONTRACT_NOT_READY", "; ".join(contract.blocking_reasons) or "contract not ready",
        )

    _require_available()

    from app.applications import browser_assist
    from app.applications.browser_runtime import BrowserRuntimeUnavailable, _get_live

    original_headless = config.BROWSER_HEADLESS
    try:
        config.BROWSER_HEADLESS = headless  # type: ignore[misc]
        result = browser_assist.start_session(execution_id)
    finally:
        config.BROWSER_HEADLESS = original_headless

    if not result.get("created"):
        return SubmitAttemptResult(SubmitOutcome.BLOCKED, "SESSION_NOT_CREATED", result.get("reason", ""))
    session = result.get("session") or {}
    session_id = session.get("session_id", "")
    status = session.get("status", "")

    try:
        if status != "READY_FOR_FINAL_SUBMIT":
            detail = f"session did not reach READY_FOR_FINAL_SUBMIT (status={status}) -- never bypassed"
            outcome = SubmitOutcome.BLOCKED
            error_type = status or "NOT_READY"
            _finish(execution_id, job, outcome, error_type, detail)
            return SubmitAttemptResult(outcome, error_type, detail)

        # Fresh, immediately-before-click contract re-check (stale form
        # fingerprint / stale approval caught here even if the browser
        # session itself looked healthy).
        fresh_contract = build_submit_contract(job_id)
        if fresh_contract is None or not fresh_contract.ready:
            detail = "; ".join(fresh_contract.blocking_reasons) if fresh_contract else "job no longer exists"
            _finish(execution_id, job, SubmitOutcome.BLOCKED, "CONTRACT_STALE", detail)
            return SubmitAttemptResult(SubmitOutcome.BLOCKED, "CONTRACT_STALE", detail)

        try:
            live = _get_live(session_id)
        except BrowserRuntimeUnavailable as exc:
            _finish(execution_id, job, SubmitOutcome.SUBMISSION_STATUS_UNKNOWN, "SESSION_LOST", str(exc))
            return SubmitAttemptResult(SubmitOutcome.SUBMISSION_STATUS_UNKNOWN, "SESSION_LOST", str(exc))

        if _test_route_hook is not None:
            live.run(_test_route_hook, live.page, timeout=15)

        candidates = live.run(_scan_final_submit_controls, live, timeout=30)
        if len(candidates) != 1:
            detail = (
                "no FINAL_SUBMIT-classified control found on the page" if not candidates
                else f"{len(candidates)} FINAL_SUBMIT-classified controls found -- cannot uniquely identify the "
                     "submit action"
            )
            _finish(execution_id, job, SubmitOutcome.BLOCKED, "SUBMIT_CONTROL_NOT_UNIQUE", detail)
            return SubmitAttemptResult(SubmitOutcome.BLOCKED, "SUBMIT_CONTROL_NOT_UNIQUE", detail)
        control = candidates[0]

        # --- submit-once claim: the LAST thing acquired before the physical
        # click, so a race loser never opens/clicks anything. ---------------
        attempt = claim.acquire_submit_claim(execution_id, job_id, claimed_by=_WORKER_ID)
        if not attempt.acquired:
            _finish(execution_id, job, SubmitOutcome.BLOCKED, "ALREADY_ATTEMPTED", attempt.reason,
                     record_claim=False)
            return SubmitAttemptResult(SubmitOutcome.BLOCKED, "ALREADY_ATTEMPTED", attempt.reason)

        pre_field_count = live.run(_count_fillable_fields, live, timeout=10)

        repo.log_event(execution_id, job_id, "submit_attempted", detail="greenhouse_submit_engine")
        observation = live.run(
            _click_and_observe, live, control,
            click_timeout_ms=config.GREENHOUSE_SUBMIT_CLICK_TIMEOUT_MS,
            observe_timeout_ms=config.BROWSER_DOM_STABILIZATION_TIMEOUT_MS,
            observe_poll_ms=config.BROWSER_DOM_STABILIZATION_POLL_MS,
            pre_field_count=pre_field_count,
            timeout=((config.GREENHOUSE_SUBMIT_CLICK_TIMEOUT_MS + config.BROWSER_DOM_STABILIZATION_TIMEOUT_MS)
                     / 1000.0) + 20,
        )

        if not observation["clicked"]:
            detail = f"timed out attempting to click the submit control -- no click was ever dispatched: " \
                     f"{observation['click_error']}"
            outcome = SubmitOutcome.SUBMISSION_STATUS_UNKNOWN
            _finish(execution_id, job, outcome, "TIMEOUT_BEFORE_CLICK", detail)
            return SubmitAttemptResult(outcome, "TIMEOUT_BEFORE_CLICK", detail)

        if not is_allowed_host_for_session(job.provider or "", session.get("application_url", ""), observation["url"]):
            detail = f"post-click navigation left the allowed provider domain: {observation['url']}"
            _finish(execution_id, job, SubmitOutcome.BLOCKED, "PLATFORM_POLICY_RESTRICTED", detail)
            return SubmitAttemptResult(SubmitOutcome.BLOCKED, "PLATFORM_POLICY_RESTRICTED", detail)

        if observation.get("failed_requests"):
            detail = "the submit request failed at the network level after the control was clicked -- outcome unknown"
            outcome = SubmitOutcome.SUBMISSION_STATUS_UNKNOWN
            _finish(execution_id, job, outcome, "CONNECTION_LOST", detail)
            return SubmitAttemptResult(outcome, "CONNECTION_LOST", detail)

        if observation["timed_out"]:
            detail = "the submit control was clicked but no response was observed before timeout -- outcome unknown"
            outcome = SubmitOutcome.SUBMISSION_STATUS_UNKNOWN
            _finish(execution_id, job, outcome, "TIMEOUT_AFTER_CLICK", detail)
            return SubmitAttemptResult(outcome, "TIMEOUT_AFTER_CLICK", detail)

        body_text = observation["body_text"]
        parsed = parse_confirmation_text(body_text, heading_text=observation.get("heading_text", ""))

        if parsed.already_applied:
            detail = f"duplicate-application evidence observed: '{parsed.matched_duplicate_phrase}' -- never " \
                      "folded into a fresh confirmation"
            outcome = SubmitOutcome.BLOCKED
            _finish(execution_id, job, outcome, "DUPLICATE_APPLICATION_DETECTED", detail)
            return SubmitAttemptResult(outcome, "DUPLICATE_APPLICATION_DETECTED", detail)

        if observation["validation_errors"]:
            detail = "server-side validation error(s): " + "; ".join(observation["validation_errors"][:5])
            outcome = SubmitOutcome.REJECTED
            _finish(execution_id, job, outcome, "SERVER_VALIDATION_ERROR", detail)
            return SubmitAttemptResult(outcome, "SERVER_VALIDATION_ERROR", detail)

        grade = classify_confirmation_evidence(
            phrase_matched=parsed.phrase_matched, confirmation_id=parsed.confirmation_id,
            current_url=observation["url"], heading_phrase_matched=parsed.heading_phrase_matched,
            submit_control_disappeared=observation.get("submit_control_disappeared"),
            form_fields_disappeared=observation.get("form_fields_disappeared"),
        )
        if grade.confirms():
            sr = SubmitAttemptResult(
                SubmitOutcome.CONFIRMED, "", grade.reason, confirmation_id=parsed.confirmation_id,
                confirmation_url=observation["url"], confirmation_text_fingerprint=parsed.text_fingerprint,
            )
            _finish(execution_id, job, SubmitOutcome.CONFIRMED, "", grade.reason,
                     confirmation_id=parsed.confirmation_id, confirmation_url=observation["url"],
                     confirmation_text_fingerprint=parsed.text_fingerprint)
            return sr

        detail = "no recognized confirmation, duplicate, or validation-error evidence on the resulting page"
        outcome = SubmitOutcome.SUBMISSION_STATUS_UNKNOWN
        _finish(execution_id, job, outcome, "UNRECOGNIZED_OUTCOME", detail)
        return SubmitAttemptResult(outcome, "UNRECOGNIZED_OUTCOME", detail)
    finally:
        try:
            browser_assist.close_session(session_id, reason="greenhouse_submit_engine finished")
        except Exception:  # noqa: BLE001 -- cleanup must never raise
            pass


def _finish(
    execution_id: str, job: Job, outcome: SubmitOutcome, error_type: str, detail: str, *,
    record_claim: bool = True, confirmation_id: str = "", confirmation_url: str = "",
    confirmation_text_fingerprint: str = "",
) -> None:
    """Persists the outcome through the SAME machinery every other
    submission path in this project already uses -- never a parallel
    receipt/state system."""
    if record_claim:
        claim.record_outcome(execution_id, outcome=outcome.value, detail=detail)

    if outcome == SubmitOutcome.CONFIRMED:
        blockers.resolve_blocker(execution_id, resolution_note="application confirmed via greenhouse canary")
        repo.update_execution(
            execution_id, job.id, ExecutionStatus.APPLIED, confirmation_id=confirmation_id,
            confirmation_url=confirmation_url, confirmation_text_fingerprint=confirmation_text_fingerprint,
            submission_method="greenhouse_canary",
        )
        repo.log_event(execution_id, job.id, "confirmed", detail=confirmation_id)
        try:
            from app.applications import approval as _approval

            latest_approval = _approval.get_latest_approval(execution_id)
            receipts.record_receipt(
                execution_id=execution_id, job_id=job.id, provider="greenhouse",
                submitted_via="greenhouse_canary", confirmation_id=confirmation_id,
                sanitized_url=confirmation_url, evidence_strength="STRONG" if confirmation_id else "MODERATE",
                raw_message_fingerprint=confirmation_text_fingerprint,
                approval_id=latest_approval["approval_id"] if latest_approval else "",
            )
        except Exception:  # noqa: BLE001 -- a receipt failure must never turn a genuine confirmation into an error
            logger.exception("failed to record greenhouse canary receipt for execution %s", execution_id)
        return

    if outcome == SubmitOutcome.REJECTED:
        repo.update_execution(
            execution_id, job.id, ExecutionStatus.PERMANENT_SUBMISSION_FAILURE, error_type=error_type,
            error_message_safe=detail[:500], submission_method="greenhouse_canary",
        )
        repo.log_event(execution_id, job.id, "failed", detail=error_type)
        return

    if outcome == SubmitOutcome.SUBMISSION_STATUS_UNKNOWN:
        repo.update_execution(
            execution_id, job.id, ExecutionStatus.SUBMISSION_STATUS_UNKNOWN, requires_user_action=1,
            user_action_reason=detail[:500], error_type=error_type, submission_method="greenhouse_canary",
        )
        repo.log_event(execution_id, job.id, "failed", detail=f"SUBMISSION_STATUS_UNKNOWN:{error_type}")
        blockers.raise_blocker(
            execution_id, job.id, blockers.BlockerCode.SUBMISSION_STATUS_UNKNOWN, provider="greenhouse",
            detail=detail, source="greenhouse_submit_engine",
        )
        return

    # BLOCKED: a decisive, evidence-based refusal (CAPTCHA/login/stale
    # approval/duplicate/etc). Never a submission attempt of any kind, so the
    # execution is left exactly as browser_assist's own pause/blocker
    # machinery already recorded it (the pause status itself IS the record)
    # -- this function only additionally logs the refusal for audit history.
    repo.log_event(execution_id, job.id, "user_action_required", detail=f"greenhouse_canary_blocked:{error_type}")
