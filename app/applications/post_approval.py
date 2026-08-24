"""Provider Post-Approval Execution V1: the bridge from a durable APPROVE &
APPLY action to the real, visible browser-assist session, for the six named
providers (Greenhouse/Lever/Ashby/Workday/SmartRecruiters/Workable) whose
`ApplicationProvider` capability is honestly ASSIST_ONLY/UNSUPPORTED (see
docs/provider-post-approval-execution-v1.md section 1 for why none of them
may honestly claim automated final-submission support).

Today, `app.applications.approval.approve_and_apply()` re-runs
`app.applications.executor.process_execution(approved=True)`, and for these
providers that lands the execution on ExecutionStatus.APPROVED and stops --
a human then has to separately remember to click "Start Browser Assist" on
the job detail page. This module closes that gap: called immediately after
`approve_and_apply()`'s own pipeline call, it opens (or resumes) the job's
browser-assist session automatically, so approval genuinely starts the
strongest safe automation right away, matching the product's desired
post-approval runtime.

This is deliberately a thin, best-effort ORCHESTRATION step, not a new
safety mechanism of its own:
  - `app.applications.browser_assist.start_session()`/`resume_session()`
    independently re-derive eligibility, job identity, CAPTCHA/login/legal
    detection, and every other gate every time they are called, regardless
    of who calls them or why -- this module adds no new authority and
    bypasses nothing.
  - Gated on `config.BROWSER_ASSIST_ENABLED` (off by default) exactly like
    every other browser-assist entry point in this project -- approval
    itself is never blocked or slowed by this being unavailable; a fresh
    call to `app.applications.executor.process_execution()`'s own
    `_approved_submit_permitted` gate has already run first and is
    untouched by this module.
  - Never starts a session for more than the ONE job that was just
    APPROVE & APPLY-ed (see docs/provider-post-approval-execution-v1.md
    section 3, "no unattended background auto-starting of browser sessions
    across multiple jobs").
  - Failures here (Playwright not installed, browser launch failure, a
    real page-level pause) are reported back for display but never raised
    into `approve_and_apply()` -- the approval itself already fully
    succeeded before this module is ever reached; a browser-assist hiccup
    must never look like the approval failed."""

from typing import Optional

from app import config
from app.applications import browser_session
from app.applications.models import ExecutionStatus


def advance_after_approval(execution_id: str) -> dict:
    """Best-effort: attempts to open/resume the browser-assist session for
    an execution that just landed on ExecutionStatus.APPROVED (a real
    provider with no verified automated final-submission capability).
    Returns {"attempted": bool, "started": bool, "session": dict|None,
    "reason": str} -- never raises."""
    if not config.BROWSER_ASSIST_ENABLED:
        return {"attempted": False, "started": False, "session": None,
                "reason": "BROWSER_ASSIST_ENABLED is false -- browser assist was not auto-started"}

    from app.applications import repo as _executions_repo

    execution = _executions_repo.get_execution(execution_id)
    if execution is None or execution.get("status") != ExecutionStatus.APPROVED.value:
        return {"attempted": False, "started": False, "session": None,
                "reason": "execution is not in the APPROVED state -- nothing to bridge"}

    job_id = execution["job_id"]
    existing = browser_session.get_active_session_for_job(job_id)
    try:
        from app.applications import browser_assist

        if existing is not None:
            result = browser_assist.resume_session(existing["session_id"])
            return {"attempted": True, "started": bool(result.get("ok")), "session": result.get("session"),
                    "reason": result.get("detail", "")}

        result = browser_assist.start_session(execution_id)
        return {"attempted": True, "started": bool(result.get("created")), "session": result.get("session"),
                "reason": result.get("reason", "")}
    except Exception as exc:  # noqa: BLE001 -- a browser-assist hiccup must never look like approval failed
        return {"attempted": True, "started": False, "session": None,
                "reason": f"browser-assist auto-start failed: {type(exc).__name__}: {exc}"}


def active_session_for_job(job_id: int) -> Optional[dict]:
    """Convenience read for callers (UI/tests) that just want to know
    whether a browser-assist session already exists for this job."""
    return browser_session.get_active_session_for_job(job_id)
