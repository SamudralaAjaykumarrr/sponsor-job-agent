"""Application-lifecycle-exception-resume-v1 Demo / Test Mode: eight
deterministic scenarios built entirely on the existing `mock_ats` fixture
mechanism (app.applications.mock_ats's `mock_scenario` provider_metadata
switch) so the whole "Jobs -> Easy Apply -> progress -> Needs Action ->
resolve -> resume -> Ready to Apply -> Approve & Apply -> mock submission ->
confirmed -> receipt" loop can be exercised without ever touching a real
employer. Every fixture job is `is_test_fixture=1` with a fixed,
deterministic `external_job_id` (`demo-fixture-<key>`) -- idempotently
upserted, matching the existing `agent-test-mode-fixture-1`/
`benchmark-fixture` never-collide-with-a-real-identifier convention -- and
is excluded from the real consumer board (app.applications.board) and the
existing ops Applications page, exactly like every other test fixture.

Submission for the "happy path" demos goes through the REAL
app.applications.approval.approve_and_apply() (APPROVE & APPLY) -- never a
bypass/override of AUTO_SUBMIT_ENABLED -- because MockATSProvider already
has submission_supported=True and the approval-gated path
(_approved_submit_permitted) never requires that flag, matching
app.agent.orchestrator's own choice to never raise AUTO_SUBMIT_ENABLED, even
in TEST MODE."""

import json
from dataclasses import dataclass

from app import config
from app.applications import approval as approval_mod
from app.applications import blockers, repo
from app.applications.executor import process_execution, queue_application
from app.applications.models import ExecutionStatus
from app.applications.reconcile import reconcile_execution
from app.applications.provider_registry import get_application_provider
from app.jobs_repo import get_job, get_job_by_provider_external_id
from app.models import ApplicationMode, Job
from app.pipeline import ingest_and_process

PROVIDER_NAME = "mock_ats"


@dataclass(frozen=True)
class DemoScenario:
    key: str
    label: str
    description: str
    mock_scenario: str
    resolve_kind: str  # "" (terminal/no action needed), "retry" (flip to simple + retry), "reconcile"


SCENARIOS: list[DemoScenario] = [
    DemoScenario("successful_application", "Demo Successful Application",
                 "A clean run all the way to an approved, mock-submitted application.", "simple", ""),
    DemoScenario("job_expired", "Demo Job Expired",
                 "The employer posting is detected as expired during preparation -- stops safely, never submits.",
                 "job_expired", ""),
    DemoScenario("login_required", "Demo Login Required",
                 "The employer's application requires signing in before it can continue.", "login_required", "retry"),
    DemoScenario("email_verification", "Demo Email Verification",
                 "The employer requires verifying your email before continuing.", "email_verification", "retry"),
    DemoScenario("captcha", "Demo CAPTCHA",
                 "The employer's application presents a CAPTCHA that must be completed by a human.",
                 "captcha", "retry"),
    DemoScenario("unknown_question", "Demo Unknown Question",
                 "The employer asks a custom question we can't answer for you.", "unknown_question", "retry"),
    DemoScenario("submission_unknown", "Demo Submission Unknown",
                 "A submission attempt times out -- we never resubmit blindly, only check status.",
                 "timeout_after_submit", "reconcile"),
    DemoScenario("confirmed_submission", "Demo Confirmed Submission",
                 "Shows the full Ready to Apply -> Approve & Apply -> confirmed -> receipt loop.", "simple", ""),
    # Apply/Automation Settings V1 section 13: an EXPLICIT, deterministic
    # demonstration of the "application limit reached" experience -- never
    # relies on the other demos' own real submit-attempt counts (see
    # app.applications.rate_limit's demo-isolation fix, which excludes every
    # is_test_fixture job from real rate-limit counting so the 8 scenarios
    # above can never collide on a shared limit). This one instead wraps its
    # own Approve & Apply in a short-lived, fully restored
    # MAX_APPLICATIONS_PER_COMPANY_PER_DAY=0 override -- see run_demo.
    DemoScenario("application_limit", "Demo Application Limit Reached",
                 "Shows the friendly 'application limit reached' experience via a temporarily simulated "
                 "(never real) limit -- your actual application limits are never touched.", "simple", ""),
]

_BY_KEY: dict[str, DemoScenario] = {s.key: s for s in SCENARIOS}


def get_scenario(key: str) -> DemoScenario:
    if key not in _BY_KEY:
        raise ValueError(f"unknown demo scenario '{key}'")
    return _BY_KEY[key]


def _external_id(key: str) -> str:
    return f"demo-fixture-{key}"


def ensure_demo_job(key: str) -> Job:
    """Idempotent -- matched by the scenario's fixed external_job_id, never
    re-seeded once it exists (mirrors app.agent.orchestrator's
    _seed_test_fixture_if_needed exactly)."""
    scenario = get_scenario(key)
    external_id = _external_id(key)
    existing = get_job_by_provider_external_id(PROVIDER_NAME, external_id)
    if existing is not None:
        return existing

    job = Job(
        title=f"Demo Backend Engineer -- {scenario.label}",
        company="Demo Fixture Co",
        location="Remote - US",
        description=(
            "We are hiring a Backend Software Engineer to build REST APIs in Python using FastAPI, "
            "with PostgreSQL, Docker, and CI/CD pipelines. This is a full-time position. "
            "H-1B sponsorship is available for this role. (Demo/test-mode fixture -- never a real employer.)"
        ),
        employment_type="Full-time",
        provider=PROVIDER_NAME,
        external_job_id=external_id,
        url=f"https://mock-ats.local/jobs/{external_id}",
        provider_metadata=json.dumps({"mock_scenario": scenario.mock_scenario}),
        mode=ApplicationMode.ASSIST,
        is_test_fixture=True,
    )
    ingest_and_process(job)
    return get_job_by_provider_external_id(PROVIDER_NAME, external_id)


def _with_executor_enabled(fn):
    """Temporarily enables APPLICATION_EXECUTOR_ENABLED for the duration of
    one demo call, restoring the operator's real value after -- same
    narrowly-scoped override pattern app.agent.orchestrator._apply_config_overrides
    uses, and never touches AUTO_SUBMIT_ENABLED."""
    prev = config.APPLICATION_EXECUTOR_ENABLED
    config.APPLICATION_EXECUTOR_ENABLED = True
    try:
        return fn()
    finally:
        config.APPLICATION_EXECUTOR_ENABLED = prev


def _with_config_override(attr: str, value, fn):
    """Same narrowly-scoped, always-restored override pattern as
    _with_executor_enabled above, generalized to one arbitrary config
    attribute -- used only by the "application_limit" demo scenario to
    deterministically demonstrate a blocked application without ever
    touching the operator's real, persisted limit."""
    prev = getattr(config, attr)
    setattr(config, attr, value)
    try:
        return fn()
    finally:
        setattr(config, attr, prev)


def run_demo(key: str) -> dict:
    """Ensures the fixture job exists, queues it, and runs one pipeline pass
    (ASSIST mode -- never auto-submits; only APPROVE & APPLY, a separate
    explicit action, can ever unlock submission).

    The "application_limit" scenario is the one exception: it demonstrates
    the "application limit reached" experience in a single click, so its
    approval step (still the real app.applications.approval.approve_and_apply(),
    never a bypass) runs here too, wrapped in the short-lived, always-restored
    MAX_APPLICATIONS_PER_COMPANY_PER_DAY=0 override -- see
    _with_config_override. No other scenario is affected: their own Approve &
    Apply is still the ordinary, unmodified /jobs/{job_id}/applications/approve
    action a real job would use."""
    get_scenario(key)  # raises ValueError for an unknown key

    def _run():
        job = ensure_demo_job(key)
        existing = repo.get_active_execution_for_job(job.id)
        if existing is None:
            result = queue_application(job.id, mode="ASSIST")
            execution_id = result.execution_id
        else:
            execution_id = existing["execution_id"]
        if execution_id:
            process_execution(execution_id)
        return job

    def _run_and_approve():
        job = _run()
        approval_mod.approve_and_apply(job.id)
        return job

    if key == "application_limit":
        job = _with_executor_enabled(
            lambda: _with_config_override("MAX_APPLICATIONS_PER_COMPANY_PER_DAY", 0, _run_and_approve)
        )
    else:
        job = _with_executor_enabled(_run)
    return describe_demo(key, job.id)


def resolve_demo(key: str) -> dict:
    """Simulates the user completing the out-of-band step (signing in,
    verifying email, solving a CAPTCHA, answering the question) -- gated
    strictly to `is_test_fixture=1` + provider == mock_ats fixtures, so this
    can never touch a real job. For "retry" scenarios this flips the
    fixture's own scenario flag to "simple" (the condition is now resolved)
    and re-runs the pipeline; for "reconcile" it uses the REAL
    provider.check_submission_status() -> reconcile_execution() mechanism,
    never a fabricated outcome."""
    scenario = get_scenario(key)
    job = get_job_by_provider_external_id(PROVIDER_NAME, _external_id(key))
    if job is None:
        raise ValueError(f"demo job for '{key}' has not been run yet")
    if not job.is_test_fixture or (job.provider or "").lower() != PROVIDER_NAME:
        raise ValueError("refusing to resolve a non-fixture job through the demo path")

    execution = repo.get_active_execution_for_job(job.id)

    if scenario.resolve_kind == "retry":
        from app.jobs_repo import update_job

        if key == "unknown_question":
            # Answering the question must never change the form's own shape
            # (that would be a genuine, separately-detected form-schema-
            # change condition, not "the user answered") -- see
            # MockATSProvider.validate()'s `demo_answered` flag.
            update_job(job.id, provider_metadata=json.dumps(
                {"mock_scenario": scenario.mock_scenario, "demo_answered": True}))
        else:
            update_job(job.id, provider_metadata=json.dumps({"mock_scenario": "simple"}))
        if execution is not None:
            _with_executor_enabled(lambda: process_execution(execution["execution_id"]))
    elif scenario.resolve_kind == "reconcile":
        if execution is not None and execution["status"] == ExecutionStatus.SUBMISSION_STATUS_UNKNOWN.value:
            fresh_job = get_job(job.id)
            provider = get_application_provider(fresh_job)
            evidence = provider.check_submission_status(fresh_job, execution)
            if evidence is not None and evidence.confirmed:
                reconcile_execution(execution["execution_id"], "confirmed_applied",
                                     confirmation_id=evidence.confirmation_id,
                                     confirmation_url=evidence.confirmation_url, note="resolved via /demo")
    # scenario.resolve_kind == "" (terminal, or the happy-path demos which
    # resolve via the real Approve & Apply action, not this endpoint) -- no-op.
    return describe_demo(key, job.id)


def _latest_execution_for_job(job_id: int) -> dict | None:
    """Active-or-terminal -- a terminal (e.g. JOB_EXPIRED) execution's
    `active` flag flips to 0, but the demo page must still show its final
    state rather than silently reverting to 'not run yet'."""
    active = repo.get_active_execution_for_job(job_id)
    if active is not None:
        return active
    executions = repo.list_executions_for_job(job_id)
    return executions[-1] if executions else None


def describe_demo(key: str, job_id: int) -> dict:
    scenario = get_scenario(key)
    job = get_job(job_id)
    execution = _latest_execution_for_job(job_id) if job else None
    latest_blocker = blockers.get_active_blocker_for_execution(execution["execution_id"]) if execution else None
    approval_freshness = approval_mod.check_approval_freshness(job_id) if job else {"has_approval": False}
    return {
        "key": scenario.key, "label": scenario.label, "description": scenario.description,
        "resolve_kind": scenario.resolve_kind, "job_id": job_id,
        "execution": execution, "blocker": latest_blocker, "approval": approval_freshness,
    }


def list_demo_status() -> list[dict]:
    out = []
    for scenario in SCENARIOS:
        job = get_job_by_provider_external_id(PROVIDER_NAME, _external_id(scenario.key))
        if job is None:
            out.append({"key": scenario.key, "label": scenario.label, "description": scenario.description,
                        "resolve_kind": scenario.resolve_kind, "job_id": None, "execution": None,
                        "blocker": None, "approval": {"has_approval": False}})
        else:
            out.append(describe_demo(scenario.key, job.id))
    return out
