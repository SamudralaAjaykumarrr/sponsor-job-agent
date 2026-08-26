"""Controlled Greenhouse submission canary -- the ONLY sanctioned way to
invoke `app.applications.greenhouse_submit_engine` against a real posting.

This is deliberately the most heavily gated code path in the entire
project. Every gate below is checked, in order, BEFORE a browser is ever
opened, and any single failed gate refuses outright -- there is no partial
or "best effort" canary run:

  1. `config.GREENHOUSE_SUBMIT_CANARY_ENABLED` must be explicitly true
     (default false, exactly like `REAL_ATS_CANARY_ENABLED`).
  2. The caller must pass `confirm=True` explicitly for THIS call -- a
     second, independent intent signal so no generic tooling/dashboard
     action can trigger a real submission by accident.
  3. The job must exist, be a recognized Greenhouse posting (canonical
     identity VERIFIED), and have a current, ACTIVE, non-stale durable
     approval (`app.applications.approval.verify_durable_approval_for_submission`)
     -- a human has already reviewed and approved this exact application.
  4. Playwright must be installed and `BROWSER_ASSIST_ENABLED` must be true.

The browser is ALWAYS visible (`headless=False`), regardless of
`config.BROWSER_HEADLESS` -- CLAUDE.md's "visible browser only" requirement
for any real canary run, enforced here rather than left to the caller.

This module never runs in a batch, never runs on any schedule, and is never
imported by `app.applications.background_scheduler`,
`app.applications.scheduler`, or any worker loop -- `run_greenhouse_submit_
canary()` always takes exactly one `job_id` and is only ever invoked by an
explicit operator action (the CLI `greenhouse-canary` command). No test in
this project may set `GREENHOUSE_SUBMIT_CANARY_ENABLED = True` -- tests
exercise the engine and the gates directly instead (see
tests/test_greenhouse_canary.py, tests/test_greenhouse_submit_engine.py)."""

from dataclasses import dataclass

from app import config
from app.applications import approval as _approval
from app.applications import repo as _repo
from app.applications.providers_greenhouse import canonical_identity
from app.jobs_repo import get_job


class CanaryDisabled(Exception):
    """GREENHOUSE_SUBMIT_CANARY_ENABLED is false, or `confirm=True` was not
    explicitly passed."""


class CanaryNotAuthorized(Exception):
    """The job/execution does not have a current, verified, approved,
    submit-ready state -- this canary NEVER bypasses that, no matter how it
    is invoked."""


@dataclass
class CanaryGateResult:
    allowed: bool
    reason: str = ""

    def as_dict(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason}


def check_gates(job_id: int, *, confirm: bool) -> CanaryGateResult:
    """Pure, side-effect-free gate check -- never opens a browser. Exposed
    separately from `run_greenhouse_submit_canary` so the CLI/dashboard can
    show an honest pre-flight readiness message without risking a real
    attempt."""
    if not config.GREENHOUSE_SUBMIT_CANARY_ENABLED:
        return CanaryGateResult(False, "GREENHOUSE_SUBMIT_CANARY_ENABLED is false")
    if not confirm:
        return CanaryGateResult(False, "explicit confirm=True was not supplied for this specific call")

    job = get_job(job_id)
    if job is None:
        return CanaryGateResult(False, f"job {job_id} not found")
    if (job.provider or "").lower() != "greenhouse":
        return CanaryGateResult(False, "job's provider is not greenhouse")

    identity = canonical_identity(job)
    if not identity.recognized:
        return CanaryGateResult(False, f"canonical Greenhouse identity not recognized: {identity.reason}")

    execution = _repo.get_active_execution_for_job(job_id)
    if execution is None:
        return CanaryGateResult(False, "no active application execution exists for this job")

    approved_ok, approved_reason = _approval.verify_durable_approval_for_submission(job, execution)
    if not approved_ok:
        return CanaryGateResult(False, f"no current authorization to submit: {approved_reason}")

    from app.applications import greenhouse_submit_engine

    try:
        greenhouse_submit_engine._require_available()
    except greenhouse_submit_engine.EngineUnavailable as exc:
        return CanaryGateResult(False, str(exc))

    return CanaryGateResult(True, "all canary gates passed")


def run_greenhouse_submit_canary(job_id: int, *, confirm: bool = False) -> dict:
    """The one entry point that may actually perform a real submit attempt.
    `confirm` defaults to False on purpose -- an accidental call with no
    arguments always refuses."""
    gate = check_gates(job_id, confirm=confirm)
    if not gate.allowed:
        if "GREENHOUSE_SUBMIT_CANARY_ENABLED" in gate.reason or "confirm=True" in gate.reason:
            raise CanaryDisabled(gate.reason)
        raise CanaryNotAuthorized(gate.reason)

    from app.applications.greenhouse_submit_engine import run_greenhouse_submit

    # CLAUDE.md: visible browser only for a real canary run, regardless of
    # the operator's ordinary BROWSER_HEADLESS setting.
    result = run_greenhouse_submit(job_id, headless=False)
    return result.as_dict()
