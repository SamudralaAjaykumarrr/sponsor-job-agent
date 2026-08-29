import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import apply_settings, config, notifications
from app.agent import state as agent_state
from app.agent import run_state as agent_run_state
from app.agent.orchestrator import orchestrator as agent_orchestrator
from app.agent.scheduler import scheduler
from app.applications import approval as applications_approval
from app.applications.cta import compute_apply_cta
from app.applications import doctor as applications_doctor
from app.applications import metrics as applications_metrics
from app.applications import repo as applications_repo
from app.applications.eligibility import evaluate_executor_eligibility
from app.matching.employment_type import resolve_employment_type_evidence
from app.applications.product_state import compute_stage
from app.applications.executor import (
    AutoSubmitDisabledError,
    ExecutorDisabledError,
    process_execution,
    queue_application,
)
from app.applications.reconcile import reconcile_execution
from app.applications.tracker import can_transition, valid_manual_transitions
from app.applications import budget as applications_budget
from app.applications import capability_matrix as applications_capability_matrix
from app.applications import circuit as applications_circuit
from app.applications import attempts as applications_attempts
from app.applications import scheduler as applications_scheduler
from app.applications import reconcile_worker as applications_reconcile_worker
from app.applications import browser_assist as applications_browser_assist
from app.applications import browser_session
from app.applications.background_scheduler import background_scheduler as applications_background_scheduler
from app.applications.worker_admin import request_drain as application_request_drain
from app.applications.worker_admin import resume_from_drain as application_resume_from_drain
from app.applications.worker_capabilities import WorkerCapability, has_capability as application_worker_has_capability
from app.applications import blockers as applications_blockers
from app.applications import board as applications_board
from app.applications import demo as applications_demo
from app.applications import presubmit_manifest
from app.applications import handoff as applications_handoff
from app.applications import recruiter_communication as applications_recruiter
from app.candidate.profile import load_profile, missing_fields
from app.config import BASE_DIR
from app.db import init_db
from app.jobs_repo import (
    get_job,
    get_state_history,
    list_discovery_cycles,
    list_discovery_log,
    list_jobs,
    list_provenance,
    record_state_change,
)
from app.models import ApplicationMode, ApplicationState, Job
from app.pipeline import generate_assist_outputs, ingest_and_process
from app.providers.registry import all_capabilities, all_provider_names
from app.resume_optimizer import doctor as resume_optimizer_doctor
from app.resume_optimizer import metrics as resume_optimizer_metrics
from app.resume_optimizer import repo as resume_optimizer_repo
from app.resume_optimizer.jd_analysis import analyze_jd as run_jd_analysis
from app.resume_optimizer.fingerprint import compute_jd_fingerprint
from app.resume_optimizer.optimizer import optimize_resume
from app.resume_optimizer.priority import compute_alignment_priority
from app.resume_optimizer.scheduler import resume_optimization_scheduler
from app.registry.models import CompanyRegistryEntry
from app.registry.repo import insert_entry, list_entries, provider_health_summary, seed_demo_entries
from app.registry.scheduling import compute_health

from app.registry import acquisition as registry_acquisition
from app.registry import analytics as registry_analytics
from app.registry import doctor as registry_doctor
from app.registry import lifecycle as registry_lifecycle
from app.registry import store as registry_store
from app.registry import sync as registry_sync
from app.registry.verification import verify_portal
from app import migrations
from app import pipeline_dashboard
from app import settings_store
from app.sponsorship import doctor as sponsorship_doctor
from app.sponsorship import metrics as sponsorship_metrics
from app.sponsorship.aliases import list_aliases_for_company
from app.sponsorship.decision import list_decision_history
from app.sponsorship.identity import list_pending_reviews as sponsorship_list_pending_reviews
from app.sponsorship.identity import resolve_review as sponsorship_resolve_review
from app.sponsorship.profile import get_or_compute_profile
from app.sponsorship.relationships import list_relationships_for_company
from app.sponsorship.review_queue import build_review_queue
from app.db import backend as db_backend
from app.health import check_readiness
from app.observability import metrics as observability_metrics
from app.version import WORKER_SOFTWARE_VERSION
from app.workers import circuit as workers_circuit
from app.workers import dead_letter as workers_dead_letter
from app.workers import leasing as workers_leasing
from app.workers import metrics as workers_metrics
from app.workers import reaper as workers_reaper
from app.workers import repo as workers_repo
from app.workers import schema_drift_repo


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    # Premium UI Settings page: re-apply any previously-saved agent tuning
    # overrides (interval/max-jobs-per-cycle/freshness-cutoff) to `config`
    # on every startup -- see app/settings_store.py. Must run after init_db()
    # (the app_settings table must exist) and before the scheduler/
    # orchestrator start reading config.* below.
    settings_store.apply_overrides_on_startup()
    # Apply/Automation Settings V1: re-applies the persisted submission-mode
    # (AUTO_SUBMIT_ENABLED)/sponsorship-policy choices on every startup, same
    # "never silently revert to the .env default" contract as the call above.
    apply_settings.apply_overrides_on_startup()
    if config.REGISTRY_SEED_DEMO_DATA:
        seed_demo_entries()
    # CLAUDE.md Phase 8 section 66: never silently enable the executor --
    # print its actual on/off state on every startup.
    print(f"Application executor: {'ON' if config.APPLICATION_EXECUTOR_ENABLED else 'OFF'}")
    print(f"Auto prepare:         {'ON' if config.APPLICATION_AUTO_PREPARE_ENABLED else 'OFF'}")
    # CLAUDE.md Phase 10 section 66: never silently enable real-browser
    # application automation -- print its actual on/off state and mode too.
    print(f"Browser assist:       {'ON' if config.BROWSER_ASSIST_ENABLED else 'OFF'}")
    print(f"Browser mode:         {'HEADLESS' if config.BROWSER_HEADLESS else 'VISIBLE'}")
    print(f"Auto submit:          {'ON' if config.AUTO_SUBMIT_ENABLED else 'OFF'}")
    # CLAUDE.md Phase 13 section 83: same "never silently enable" principle
    # extended to the canary and the job-identity gate.
    print(f"ATS canary:           {'ON' if config.REAL_ATS_CANARY_ENABLED else 'OFF'}")
    print(f"Job identity gate:    {'ON' if config.APPLICATION_IDENTITY_REQUIRED else 'OFF'}")
    # CLAUDE.md Phase 14 section 56: never silently enable background resume
    # optimization -- print its actual on/off state on every startup, same
    # as every other optional background loop in this project.
    print(f"Resume optimization:  {'ON' if config.RESUME_OPTIMIZATION_ENABLED else 'OFF'}")
    print(f"One-page resumes:     {'REQUIRED' if config.ONE_PAGE_RESUME_REQUIRED else 'not enforced'}")
    scheduler.start()
    applications_background_scheduler.start()
    resume_optimization_scheduler.start()

    # Restart recovery (CLAUDE.md one-click-agent section 42): if the
    # process restarted while the user's desired state was RUNNING, resume
    # safely -- never require the user to re-click START after a routine
    # restart, and never duplicate in-flight applications (the orchestrator's
    # own stages all reuse this project's existing idempotent/leased
    # queue-claim mechanisms regardless of how the process starts).
    recovered_state = agent_run_state.get_run_state()
    if recovered_state["desired_state"] == agent_run_state.AgentRunState.RUNNING.value:
        print("Agent:                 RECOVERING (desired state was RUNNING before restart)")
        agent_orchestrator.start(test_mode=bool(recovered_state["test_mode"]))
    else:
        print("Agent:                 STOPPED")

    yield
    await agent_orchestrator.stop()
    await scheduler.stop()
    await applications_background_scheduler.stop()
    await resume_optimization_scheduler.stop()


app = FastAPI(title="Sponsor Job Agent", lifespan=lifespan)
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
from app.freshness.tracker import freshness_label as _freshness_label
templates.env.filters["freshness_label"] = _freshness_label
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")


def _nav_ctx() -> dict:
    """Shared context every page extending templates/base.html needs for its
    topbar agent-status chip -- a thin read of the orchestrator's own status()
    (never a second/duplicate status computation) -- and its notification
    bell's unread count (one-click-application-experience-v1 section J)."""
    return {
        "orchestrator_state": agent_orchestrator.status()["actual_state"],
        "notifications_unread_count": notifications.unread_count(),
    }


FILE_FIELDS = {
    "docx": "resume_docx_path",
    "pdf": "resume_pdf_path",
    "txt": "resume_txt_path",
    "job_analysis": "job_analysis_path",
    "answers": "application_answers_path",
    "cover_letter": "cover_letter_path",
}


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    work_arrangement: str = "",
    sponsorship_status: str = "",
    application_state: str = "",
    fresh_under_1hr: bool = False,
    fresh_under_6hr: bool = False,
    high_priority: bool = False,
    historical_strength: str = "",
    resume_status: str = "",
    needs_action_only: bool = False,
    full_time_only: bool = True,
    include_test_data: bool = False,
):
    """CLAUDE.md Phase 14 sections 44-55, 79: the unified one-page dashboard
    -- summary cards, the pipeline table (with JD-coverage/resume/
    application/user-action columns sourced from cached, indexed tables,
    never recomputed live), and filters, all on one screen. Specialist pages
    (/applications, /fleet, /registry, ...) remain for admin/debugging."""

    filters = {
        "work_arrangement": work_arrangement or None,
        "sponsorship_status": sponsorship_status or None,
        "application_state": application_state or None,
        "fresh_under_1hr": fresh_under_1hr or None,
        "fresh_under_6hr": fresh_under_6hr or None,
        "high_priority": high_priority or None,
        "historical_strength": historical_strength or None,
        "resume_status": resume_status or None,
        "needs_action_only": needs_action_only or None,
        "full_time_only": full_time_only or None,
    }
    # CLAUDE.md production-v2 dashboard defect 6: synthetic/test rows (TEST
    # MODE's mock_ats fixture) are excluded from the real-mode dashboard by
    # default -- include_test_data=true is the explicit, opt-in audit view
    # for the TABLE only; summary cards/metrics always stay real-only
    # (CLAUDE.md section 68: test counters must never contaminate production
    # statistics), regardless of this toggle.
    all_jobs = list_jobs({})
    summary = pipeline_dashboard.compute_pipeline_summary(all_jobs)

    if include_test_data:
        filters["include_test_fixtures"] = True

    # CLAUDE.md Phase 15 section 42/44: bound the rendered table to the
    # top-N matching jobs (already priority-sorted) rather than rendering
    # every match -- applied last (inside build_job_rows), after every
    # filter, so resume_status/needs_action_only still search the full
    # matching set, never just the first page of it.
    row_data = pipeline_dashboard.build_job_rows(
        filters, full_time_only=full_time_only, resume_status=resume_status,
        needs_action_only=needs_action_only, row_cap=config.DASHBOARD_MAX_TABLE_ROWS,
    )
    jobs = row_data["jobs"]
    pipeline_rows = row_data["pipeline_rows"]
    total_matching = row_data["total_matching"]

    missing = missing_fields(load_profile())
    needs_action_queue = pipeline_dashboard.build_needs_action_queue(limit=25)
    ready_for_approval_queue = pipeline_dashboard.build_ready_for_approval_queue(
        limit=50, include_test_fixtures=include_test_data,
    )
    recent_activity = pipeline_dashboard.build_recent_activity(limit=20)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            **_nav_ctx(),
            "jobs": jobs, "pipeline_rows": pipeline_rows, "filters": filters,
            "summary": summary,
            "total_matching": total_matching,
            "table_row_cap": config.DASHBOARD_MAX_TABLE_ROWS,
            "missing_profile_fields": missing[:10],
            "missing_profile_count": len(missing),
            "agent": agent_state.get_status(),
            "orchestrator": agent_orchestrator.status(),
            "agent_config": {
                "interval_minutes": config.AGENT_INTERVAL_MINUTES,
                "max_jobs_per_cycle": config.MAX_JOBS_PER_CYCLE,
                "min_match_score": config.MIN_MATCH_SCORE,
                "enabled_providers": config.ENABLED_PROVIDERS,
                "application_executor_enabled": config.APPLICATION_EXECUTOR_ENABLED,
                "auto_submit_enabled": config.AUTO_SUBMIT_ENABLED,
                "sponsorship_policy": config.SPONSORSHIP_POLICY,
            },
            "resume_optimization_enabled": config.RESUME_OPTIMIZATION_ENABLED,
            "needs_action_queue": needs_action_queue,
            "ready_for_approval_queue": ready_for_approval_queue,
            "recent_activity": recent_activity,
        },
    )


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(
    request: Request,
    q: str = "",
    work_arrangement: str = "",
    sponsorship_status: str = "",
    application_state: str = "",
    resume_status: str = "",
    high_priority: bool = False,
    full_time_only: bool = True,
    fresh_under_1hr: bool = False,
    fresh_under_6hr: bool = False,
):
    """Dedicated Jobs browser: search + the same filter axes as the
    Dashboard's pipeline table, rendered as product cards (CLAUDE.md premium
    UI brief). Shares app.pipeline_dashboard.build_job_rows() with the
    Dashboard route -- never a second, duplicate row-building implementation.

    Tsenta-parity-closure-v1, P1: fresh_under_1hr/fresh_under_6hr were
    already fully supported by app.jobs_repo.list_jobs()'s filter dict (the
    Dashboard already exposed them) -- this route just wires the same two
    query params/chips in here too, matching the Dashboard exactly."""
    filters = {
        "work_arrangement": work_arrangement or None,
        "sponsorship_status": sponsorship_status or None,
        "application_state": application_state or None,
        "high_priority": high_priority or None,
        "fresh_under_1hr": fresh_under_1hr or None,
        "fresh_under_6hr": fresh_under_6hr or None,
    }
    row_data = pipeline_dashboard.build_job_rows(
        filters, full_time_only=full_time_only, resume_status=resume_status,
        row_cap=config.DASHBOARD_MAX_TABLE_ROWS, search=q,
    )
    return templates.TemplateResponse(
        request, "jobs.html",
        {
            **_nav_ctx(),
            "pipeline_rows": row_data["pipeline_rows"],
            "total_matching": row_data["total_matching"],
            "filters": {
                "q": q, "work_arrangement": work_arrangement, "sponsorship_status": sponsorship_status,
                "application_state": application_state, "resume_status": resume_status,
                "high_priority": high_priority, "full_time_only": full_time_only,
                "fresh_under_1hr": fresh_under_1hr or None, "fresh_under_6hr": fresh_under_6hr or None,
            },
        },
    )


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: int):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    score_breakdown = {}
    if job.score_breakdown:
        try:
            score_breakdown = json.loads(job.score_breakdown)
        except json.JSONDecodeError:
            score_breakdown = {}
    history = get_state_history(job_id)
    provenance = list_provenance(job_id)
    decision_history = list_decision_history(job_id)
    latest_decision = decision_history[-1] if decision_history else None
    executions = applications_repo.list_executions_for_job(job_id)
    active_execution = applications_repo.get_active_execution_for_job(job_id)
    # A terminal execution (e.g. APPLIED) is no longer "active" (active=0),
    # but its confirmation evidence should still be visible on this page --
    # fall back to the most recent execution row for that purpose only.
    latest_execution = executions[-1] if executions else None
    eligibility = evaluate_executor_eligibility(job)
    employment_type_decision = resolve_employment_type_evidence(
        job.employment_type, job.title, job.description, job.employment_type_page_evidence_raw,
    )
    active_browser_session = browser_session.get_active_session_for_job(job_id)

    # --- Phase 14: JD analysis / resume optimization diagnostics ------------
    current_variant = resume_optimizer_repo.get_current_variant(job_id)
    if current_variant is not None and isinstance(current_variant.get("compression_log"), str):
        try:
            current_variant["compression_log"] = json.loads(current_variant["compression_log"] or "[]")
        except json.JSONDecodeError:
            current_variant["compression_log"] = []
    quality_row = resume_optimizer_repo.get_quality_report_for_job(job_id)
    jd_fingerprint = compute_jd_fingerprint(job.title, job.company, job.description)
    jd_analysis_row = resume_optimizer_repo.get_jd_analysis(job_id, jd_fingerprint)
    evidence_links = resume_optimizer_repo.list_evidence_links(current_variant["variant_id"]) if current_variant else []
    alignment_priority = compute_alignment_priority(job, quality_row["report"] if quality_row else None)
    approval_freshness = applications_approval.check_approval_freshness(job_id)
    latest_receipt = None
    if latest_execution is not None:
        from app.applications import receipts as applications_receipts

        latest_receipt = applications_receipts.get_latest_receipt_for_execution(latest_execution["execution_id"])

    job_cta = compute_apply_cta(
        job_id, job.application_state.value,
        execution=active_execution, browser_session=active_browser_session,
    ).as_dict()

    return templates.TemplateResponse(
        request, "job_detail.html",
        {
            **_nav_ctx(),
            "job": job, "score_breakdown": score_breakdown, "history": history, "provenance": provenance,
            "latest_decision": latest_decision, "decision_history": decision_history,
            "executions": executions, "active_execution": active_execution,
            "latest_execution": latest_execution, "eligibility": eligibility,
            "employment_type_decision": employment_type_decision,
            "job_cta": job_cta,
            "executor_enabled": config.APPLICATION_EXECUTOR_ENABLED,
            "auto_submit_enabled": config.AUTO_SUBMIT_ENABLED,
            "active_browser_session": active_browser_session,
            "browser_assist_enabled": config.BROWSER_ASSIST_ENABLED,
            "current_variant": current_variant,
            "quality_report": quality_row["report"] if quality_row else None,
            "jd_analysis": jd_analysis_row,
            "evidence_links": evidence_links,
            "alignment_priority": alignment_priority,
            "jd_current_fingerprint": jd_fingerprint,
            "valid_states": [s.value for s in valid_manual_transitions(job.application_state)],
            "approval_freshness": approval_freshness,
            "latest_receipt": latest_receipt,
        },
    )


# --- Premium UI: Tracker / Activity / Profile / Settings --------------------

@app.get("/tracker", response_class=HTMLResponse)
def tracker_page(request: Request):
    """Applied / Assessment / Interview / Offer / Rejected / Withdrawn
    Kanban board. All six states are manual-only transitions a human makes
    from the job detail page (app.applications.tracker) -- nothing here is
    set automatically by the pipeline or executor."""
    lanes = pipeline_dashboard.build_tracker_board()
    return templates.TemplateResponse(
        request, "tracker.html", {**_nav_ctx(), "lanes": lanes},
    )


@app.get("/activity", response_class=HTMLResponse)
def activity_page(request: Request):
    """Inbox / Activity: the same company/title-free-where-appropriate,
    already-existing feed the Dashboard's Live Activity panel shows, just a
    longer, dedicated history -- plus the current Needs Your Action queue
    pinned at the top so nothing actionable is buried in the timeline."""
    needs_action_queue = pipeline_dashboard.build_needs_action_queue(limit=50)
    recent_activity = pipeline_dashboard.build_recent_activity(limit=150)
    return templates.TemplateResponse(
        request, "activity.html",
        {**_nav_ctx(), "needs_action_queue": needs_action_queue, "recent_activity": recent_activity},
    )


@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request):
    """Read-only view of the candidate's own private profile
    (candidate_data/profile.json) -- deliberately no edit form here (the
    premium-UI brief's 'do not modify private candidate data' instruction);
    editing stays exactly where it already was, directly in the JSON file."""
    profile = load_profile()
    missing = missing_fields(profile)
    return templates.TemplateResponse(
        request, "profile.html",
        {
            **_nav_ctx(),
            "profile": profile, "missing_fields": missing, "missing_count": len(missing),
            "profile_path": str(config.CANDIDATE_DIR / "profile.json"),
        },
    )


_AGENT_TUNING_KEYS = ("agent_interval_minutes", "max_jobs_per_cycle", "freshness_max_days")
_LIMIT_SETTING_KEYS = (
    "max_applications_per_day", "max_applications_per_company_per_day",
    "max_applications_per_week", "max_concurrent_applications", "min_salary_usd",
)


def _settings_context(
    *, errors: list[str] | None = None, saved_section: str = "",
    needs_confirmation: bool = False, pending_submission_mode: str = "",
) -> dict:
    """Single builder for every piece of context settings.html needs --
    used by the GET route and every POST route's error/confirmation
    re-render, so the page's sections are always populated identically
    regardless of which form was just submitted."""
    all_values = settings_store.current_values()
    apply_current = apply_settings.get_settings()
    profile = load_profile()
    return {
        **_nav_ctx(),
        "errors": errors or [],
        "saved_section": saved_section,
        "needs_confirmation": needs_confirmation,
        "pending_submission_mode": pending_submission_mode,
        "agent_tuning_values": {k: all_values[k] for k in _AGENT_TUNING_KEYS},
        "agent_tuning_specs": {k: settings_store.ALLOWED_SETTINGS[k] for k in _AGENT_TUNING_KEYS},
        "limit_values": {k: all_values[k] for k in _LIMIT_SETTING_KEYS},
        "limit_specs": {k: settings_store.ALLOWED_SETTINGS[k] for k in _LIMIT_SETTING_KEYS},
        "apply_settings": apply_current,
        "resume_modes": [m.value for m in apply_settings.ResumeOptimizationMode],
        "cover_letter_policies": [p.value for p in apply_settings.CoverLetterPolicy],
        "work_arrangement_choices": apply_settings.WORK_ARRANGEMENTS,
        "profile_work_authorization": profile.work_authorization,
        "requires_sponsorship_display": (
            "Yes" if profile.work_authorization.requires_sponsorship is True
            else "No" if profile.work_authorization.requires_sponsorship is False
            else "NEEDS_USER_INPUT"
        ),
        "safety_flags": {
            "application_executor_enabled": config.APPLICATION_EXECUTOR_ENABLED,
            "auto_submit_enabled": config.AUTO_SUBMIT_ENABLED,
            "application_auto_prepare_enabled": config.APPLICATION_AUTO_PREPARE_ENABLED,
            "browser_assist_enabled": config.BROWSER_ASSIST_ENABLED,
            "browser_headless": config.BROWSER_HEADLESS,
            "one_page_resume_required": config.ONE_PAGE_RESUME_REQUIRED,
            "resume_optimization_enabled": config.RESUME_OPTIMIZATION_ENABLED,
            "real_ats_canary_enabled": config.REAL_ATS_CANARY_ENABLED,
        },
        "enabled_providers": config.ENABLED_PROVIDERS,
        "sponsorship_policy": config.SPONSORSHIP_POLICY,
    }


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: str = ""):
    return templates.TemplateResponse(request, "settings.html", _settings_context(saved_section=saved))


@app.post("/settings")
async def settings_save(request: Request):
    """Advanced/Agent-tuning + Application-limit numeric knobs (see
    app/settings_store.py) -- a real, functional Save, never a no-op. Every
    other config flag on the Advanced Safety card is display-only by design
    (CLAUDE.md's 'never silently enable' rules for the executor/browser-
    assist flags)."""
    form = await request.form()
    keys = _AGENT_TUNING_KEYS + _LIMIT_SETTING_KEYS
    values = {key: form.get(key) for key in keys if form.get(key) is not None}
    errors = settings_store.save_settings(values)
    if errors:
        return templates.TemplateResponse(
            request, "settings.html", _settings_context(errors=errors), status_code=400,
        )
    return RedirectResponse(url="/settings?saved=true", status_code=303)


@app.post("/settings/resume")
async def settings_save_resume(request: Request):
    form = await request.form()
    result = apply_settings.save_resume_settings(dict(form))
    if not result.ok:
        return templates.TemplateResponse(request, "settings.html", _settings_context(errors=result.errors), status_code=400)
    return RedirectResponse(url="/settings?saved=resume", status_code=303)


@app.post("/settings/cover-letter")
async def settings_save_cover_letter(request: Request):
    form = await request.form()
    result = apply_settings.save_cover_letter_settings(dict(form))
    if not result.ok:
        return templates.TemplateResponse(request, "settings.html", _settings_context(errors=result.errors), status_code=400)
    return RedirectResponse(url="/settings?saved=cover-letter", status_code=303)


@app.post("/settings/submission")
async def settings_save_submission(request: Request):
    """Apply/Automation Settings V1 section 10: turning Submission from
    Review to Auto-submit requires an explicit confirmation checkbox
    (`confirm_auto_submit`) submitted on the SAME request -- omitting it
    persists nothing and re-renders the page with the confirmation prompt
    expanded instead."""
    form = await request.form()
    confirmed = str(form.get("confirm_auto_submit") or "").strip().lower() in ("1", "true", "yes", "on")
    result = apply_settings.save_submission_settings(dict(form), confirmed=confirmed)
    if result.needs_confirmation:
        return templates.TemplateResponse(
            request, "settings.html",
            _settings_context(needs_confirmation=True, pending_submission_mode=str(form.get("submission_mode") or "")),
        )
    if not result.ok:
        return templates.TemplateResponse(request, "settings.html", _settings_context(errors=result.errors), status_code=400)
    return RedirectResponse(url="/settings?saved=submission", status_code=303)


@app.post("/settings/preferences")
async def settings_save_preferences(request: Request):
    form = await request.form()
    payload = dict(form)
    # work_arrangements is a multi-select checkbox group -- form.getlist
    # (not dict(form), which keeps only the last value per key) captures
    # every checked box.
    payload["work_arrangements"] = form.getlist("work_arrangements")
    result = apply_settings.save_preferences_settings(payload)
    if not result.ok:
        return templates.TemplateResponse(request, "settings.html", _settings_context(errors=result.errors), status_code=400)
    return RedirectResponse(url="/settings?saved=preferences", status_code=303)


@app.post("/settings/sponsorship")
async def settings_save_sponsorship(request: Request):
    form = await request.form()
    result = apply_settings.save_sponsorship_settings(dict(form))
    if not result.ok:
        return templates.TemplateResponse(request, "settings.html", _settings_context(errors=result.errors), status_code=400)
    return RedirectResponse(url="/settings?saved=sponsorship", status_code=303)


@app.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request):
    """One-click-application-experience-v1 (CLAUDE.md section J): the
    notification bell's target page -- a plain, chronological list with a
    per-row and a bulk "mark all read" action. Never a second state machine:
    every row is a read-only projection of app.notifications' own durable
    log."""
    return templates.TemplateResponse(
        request, "notifications.html",
        {**_nav_ctx(), "rows": notifications.list_notifications(limit=100)},
    )


@app.post("/notifications/{notification_id}/read")
def notification_mark_read(notification_id: int, request: Request):
    notifications.mark_read(notification_id)
    if "application/json" in (request.headers.get("accept") or ""):
        return JSONResponse({"ok": True, "unread_count": notifications.unread_count()})
    return RedirectResponse(url="/notifications", status_code=303)


@app.post("/notifications/mark-all-read")
def notifications_mark_all_read(request: Request):
    notifications.mark_all_read()
    if "application/json" in (request.headers.get("accept") or ""):
        return JSONResponse({"ok": True, "unread_count": notifications.unread_count()})
    return RedirectResponse(url="/notifications", status_code=303)


@app.get("/api/notifications")
def api_notifications(unread_only: bool = False, limit: int = 20):
    return JSONResponse({
        "unread_count": notifications.unread_count(),
        "items": notifications.list_notifications(unread_only=unread_only, limit=limit),
    })


@app.get("/api/dashboard/live")
def api_dashboard_live():
    """Single aggregated, read-only JSON endpoint the Dashboard's bounded
    polling (app/static/js/app.js) reads -- purely an aggregation of
    already-existing pure functions (agent_state, agent_orchestrator,
    pipeline_dashboard), never new business logic."""
    return JSONResponse({
        "agent": agent_state.get_status(),
        "orchestrator": agent_orchestrator.status(),
        "summary": pipeline_dashboard.compute_pipeline_summary(list_jobs({})),
        "needs_action_queue": pipeline_dashboard.build_needs_action_queue(limit=8),
        "recent_activity": pipeline_dashboard.build_recent_activity(limit=10),
    })


# --- Phase 7: sponsorship intelligence dashboard ----------------------------

@app.get("/companies", response_class=HTMLResponse)
def companies_page(request: Request, search: str = ""):
    companies = registry_store.list_companies(limit=200, search=search)
    rows = []
    for c in companies:
        profile = get_or_compute_profile(c.id) if c.id else None
        rows.append({"company": c, "profile": profile})
    return templates.TemplateResponse(
        request, "companies.html", {"rows": rows, "search": search},
    )


@app.get("/companies/{company_id}", response_class=HTMLResponse)
def company_detail_page(request: Request, company_id: int):
    company = registry_store.get_company(company_id)
    if company is None:
        raise HTTPException(404, "company not found")
    profile = get_or_compute_profile(company_id)
    aliases = list_aliases_for_company(company_id)
    relationships = list_relationships_for_company(company_id)
    from app.sponsorship.evidence import list_evidence_for_company

    evidence = list_evidence_for_company(company_id, limit=50)
    return templates.TemplateResponse(
        request, "company_detail.html",
        {
            "company": company, "profile": profile, "aliases": aliases,
            "relationships": relationships, "evidence": evidence,
        },
    )


@app.get("/sponsorship/review-queue", response_class=HTMLResponse)
def sponsorship_review_queue_page(request: Request):
    items = build_review_queue(limit=200)
    return templates.TemplateResponse(request, "sponsorship_review_queue.html", {"items": items})


@app.get("/sponsorship/doctor", response_class=HTMLResponse)
def sponsorship_doctor_page(request: Request):
    report = sponsorship_doctor.run_doctor()
    return templates.TemplateResponse(request, "sponsorship_doctor.html", {"report": report})


@app.get("/sponsorship/identity-review", response_class=HTMLResponse)
def sponsorship_identity_review_page(request: Request):
    pending = sponsorship_list_pending_reviews()
    resolved_companies = {}
    for item in pending:
        for cid in item.get("candidate_company_ids", []):
            if cid not in resolved_companies:
                c = registry_store.get_company(cid)
                resolved_companies[cid] = c
    return templates.TemplateResponse(
        request, "sponsorship_identity_review.html", {"pending": pending, "companies": resolved_companies},
    )


@app.post("/sponsorship/identity-review/{review_id}/resolve")
def sponsorship_identity_review_resolve(review_id: int, company_id: str = Form("")):
    resolved_id = int(company_id) if company_id.strip() else None
    sponsorship_resolve_review(review_id, resolved_id, note="resolved via dashboard")
    return RedirectResponse(url="/sponsorship/identity-review", status_code=303)


# --- Phase 7 (CLAUDE.md section 51): safe read-only JSON API endpoints ------

@app.get("/api/companies/{company_id}/sponsorship")
def api_company_sponsorship(company_id: int):
    company = registry_store.get_company(company_id)
    if company is None:
        raise HTTPException(404, "company not found")
    profile = get_or_compute_profile(company_id)
    return JSONResponse({
        "company_id": company_id, "display_name": company.display_name,
        "primary_domain": company.primary_domain,
        "label": "HISTORICAL EVIDENCE -- NOT A GUARANTEE FOR ANY CURRENT ROLE",
        "historical_strength": profile.historical_strength.value,
        "years_with_h1b_activity": profile.years_with_h1b_activity,
        "most_recent_fiscal_year": profile.most_recent_fiscal_year,
        "recent_filing_count": profile.recent_filing_count,
        "historical_filing_count": profile.historical_filing_count,
        "continuity_years": profile.continuity_years,
        "trend": profile.trend,
        "recent_states": profile.recent_states,
        "recent_occupation_families": profile.recent_occupation_families,
        "history_score": profile.history_score,
        "history_reasons": profile.history_reasons,
        "aliases": list_aliases_for_company(company_id),
        "relationships": list_relationships_for_company(company_id),
    })


@app.get("/api/jobs/{job_id}/sponsorship")
def api_job_sponsorship(job_id: int):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    history = list_decision_history(job_id)
    return JSONResponse({
        "job_id": job_id, "current_status": job.sponsorship_status.value,
        "decision_version": job.sponsorship_decision_version,
        "conflict": bool(job.sponsorship_conflict), "blocking_reason": job.sponsorship_blocking_reason,
        "decision_history": history,
    })


@app.get("/api/sponsorship/review-queue")
def api_sponsorship_review_queue(limit: int = 100):
    items = build_review_queue(limit=limit)
    return JSONResponse([
        {
            "job_id": i.job_id, "title": i.title, "company": i.company, "location": i.location,
            "work_arrangement": i.work_arrangement, "technical_match_score": i.technical_match_score,
            "priority_score": i.priority_score, "historical_strength": i.historical_strength,
            "missing_confirmation": i.missing_confirmation, "reasons": i.reasons,
        }
        for i in items
    ])


@app.get("/api/sponsorship/datasets")
def api_sponsorship_datasets():
    from app.sponsorship.datasets import list_datasets

    return JSONResponse(list_datasets())


@app.get("/api/sponsorship/stats")
def api_sponsorship_stats():
    return JSONResponse(sponsorship_metrics.collect())


@app.get("/providers", response_class=HTMLResponse)
def providers_page(request: Request):
    health_by_provider = {h["provider"]: h for h in provider_health_summary()}
    rows = []
    for cap in all_capabilities():
        d = cap.as_dict()
        d["health"] = health_by_provider.get(cap.provider_name)
        rows.append(d)
    return templates.TemplateResponse(request, "providers.html", {"providers": rows})


@app.get("/registry", response_class=HTMLResponse)
def registry_page(
    request: Request,
    provider: str = "",
    portal_status: str = "",
    support_level: str = "",
    portal_enabled: str = "",
    search: str = "",
):
    entries = list_entries(provider=provider or None)
    rows = [
        {"entry": e, "health": compute_health(e.consecutive_failures, e.last_success_at).value}
        for e in entries
    ]

    enabled_filter = None
    if portal_enabled == "yes":
        enabled_filter = True
    elif portal_enabled == "no":
        enabled_filter = False

    portals = registry_store.list_portals(
        provider=provider, verification_status=portal_status, support_level=support_level,
        enabled=enabled_filter, search=search, limit=200,
    )
    portal_rows = []
    for p in portals:
        company = registry_store.get_company(p.company_id)
        portal_rows.append({"portal": p, "company": company})

    return templates.TemplateResponse(
        request, "registry.html",
        {
            "rows": rows, "provider_filter": provider, "provider_names": all_provider_names(),
            "portal_rows": portal_rows,
            "portal_status_filter": portal_status, "support_level_filter": support_level,
            "portal_enabled_filter": portal_enabled, "search": search,
            "snapshot": registry_analytics.snapshot(),
            "portal_statuses": ["DISCOVERED", "CANDIDATE", "VERIFIED", "ACTIVE", "DEGRADED", "STALE", "QUARANTINED", "DISABLED"],
            "support_levels": ["FULL", "PARTIAL", "EXPERIMENTAL", "UNSUPPORTED"],
        },
    )


@app.post("/registry/add")
def registry_add(
    company_name: str = Form(...),
    provider: str = Form(...),
    tenant_identifier: str = Form(...),
    careers_url: str = Form(""),
    country: str = Form(""),
):
    entry = CompanyRegistryEntry(
        company_name=company_name, provider=provider, tenant_identifier=tenant_identifier,
        careers_url=careers_url, country=country,
    )
    insert_entry(entry)
    return RedirectResponse(url="/registry", status_code=303)


@app.get("/registry/doctor", response_class=HTMLResponse)
def registry_doctor_page(request: Request):
    report = registry_doctor.run_doctor()
    return templates.TemplateResponse(request, "registry_doctor.html", {"report": report})


@app.get("/registry/portals/{portal_id}", response_class=HTMLResponse)
def portal_detail(request: Request, portal_id: int):
    portal = registry_store.get_portal(portal_id)
    if portal is None:
        raise HTTPException(404, "portal not found")
    company = registry_store.get_company(portal.company_id)
    provenance = registry_store.list_provenance_for_portal(portal_id)
    migrations = registry_lifecycle.list_migrations_for_company(portal.company_id)
    sibling_portals = [p for p in registry_store.list_portals_for_company(portal.company_id) if p.id != portal_id]
    return templates.TemplateResponse(
        request, "portal_detail.html",
        {
            "portal": portal, "company": company, "provenance": provenance,
            "migrations": migrations, "sibling_portals": sibling_portals,
        },
    )


def _portal_action_redirect(portal_id: int):
    return RedirectResponse(url=f"/registry/portals/{portal_id}", status_code=303)


@app.post("/registry/portals/{portal_id}/verify")
def portal_verify(portal_id: int):
    portal = registry_store.get_portal(portal_id)
    if portal is None:
        raise HTTPException(404, "portal not found")
    company = registry_store.get_company(portal.company_id)
    outcome = verify_portal(portal, company_display_name=company.display_name if company else "")
    registry_lifecycle.apply_verification_outcome(portal_id, outcome)
    registry_lifecycle.maybe_detect_migration(portal.company_id, registry_store.get_portal(portal_id))
    registry_sync.sync_portal_to_operational_registry(portal_id)
    return _portal_action_redirect(portal_id)


# Alias for the dashboard's "Recheck careers page" action -- same underlying
# bounded live verification, just a distinct route name for clarity in the UI.
@app.post("/registry/portals/{portal_id}/recheck")
def portal_recheck(portal_id: int):
    return portal_verify(portal_id)


@app.post("/registry/portals/{portal_id}/enable")
def portal_enable(portal_id: int):
    portal = registry_store.get_portal(portal_id)
    if portal is None:
        raise HTTPException(404, "portal not found")
    registry_store.update_portal(portal_id, enabled=True)
    registry_sync.sync_portal_to_operational_registry(portal_id)
    return _portal_action_redirect(portal_id)


@app.post("/registry/portals/{portal_id}/disable")
def portal_disable(portal_id: int):
    portal = registry_store.get_portal(portal_id)
    if portal is None:
        raise HTTPException(404, "portal not found")
    registry_store.update_portal(portal_id, enabled=False)
    registry_sync.sync_portal_to_operational_registry(portal_id)
    return _portal_action_redirect(portal_id)


@app.post("/registry/portals/{portal_id}/quarantine")
def portal_quarantine(portal_id: int):
    from app.registry.models import PortalStatus

    portal = registry_store.get_portal(portal_id)
    if portal is None:
        raise HTTPException(404, "portal not found")
    registry_store.update_portal(portal_id, verification_status=PortalStatus.QUARANTINED.value)
    registry_sync.sync_portal_to_operational_registry(portal_id)
    return _portal_action_redirect(portal_id)


@app.get("/discovery-log")
def discovery_log_view(provider: str = "", limit: int = 50):
    return JSONResponse(list_discovery_log(limit=limit, provider=provider or None))


@app.post("/jobs/ingest")
def ingest_job(
    title: str = Form(...),
    company: str = Form(...),
    location: str = Form(""),
    description: str = Form(...),
    url: str = Form(""),
    published_at: str = Form(""),
    mode: str = Form("ASSIST"),
):
    job = Job(
        title=title, company=company, location=location, description=description,
        url=url, published_at=published_at or None, mode=ApplicationMode(mode),
    )
    result = ingest_and_process(job)
    return RedirectResponse(url=f"/jobs/{result.id}", status_code=303)


@app.post("/jobs/{job_id}/state")
def update_state(job_id: int, target_state: str = Form(...)):
    from app.jobs_repo import update_job

    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    target = ApplicationState(target_state)
    if not can_transition(job.application_state, target):
        raise HTTPException(400, f"cannot transition {job.application_state} -> {target}")
    update_job(job_id, application_state=target)
    record_state_change(job_id, job.application_state.value, target.value, actor="user")
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/regenerate")
def regenerate_resume(job_id: int):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    try:
        generate_assist_outputs(job_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


# --- Phase 14: JD analysis / resume optimization -----------------------------

@app.post("/jobs/{job_id}/resume/analyze")
def resume_analyze(job_id: int):
    """CLAUDE.md section 50 'Analyze JD' -- extracts and caches the JD
    requirements model without generating a resume artifact."""
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    fingerprint = compute_jd_fingerprint(job.title, job.company, job.description)
    analysis = run_jd_analysis(job.title, job.description)
    resume_optimizer_repo.save_jd_analysis(job_id, fingerprint, analysis)
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/resume/optimize")
def resume_optimize(job_id: int, force: bool = Form(False)):
    """CLAUDE.md section 50 'Generate/Regenerate Resume'."""
    try:
        optimize_resume(job_id, force=force)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/resume/approve")
def resume_approve(job_id: int):
    """Apply/Automation Settings V1: the manual "Approve resume" action,
    used when Auto-approve resume is OFF and a generated variant is READY/
    one-page but not yet promoted onto the job -- never gated by that
    setting itself (a manual action always works, matching this project's
    existing "manual action is never gated by an automation flag"
    convention)."""
    from app.resume_optimizer.promotion import promote_current_variant

    if get_job(job_id) is None:
        raise HTTPException(404, "job not found")
    promote_current_variant(job_id)
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.get("/jobs/{job_id}/resume/download/{file_key}")
def resume_download(job_id: int, file_key: str):
    """Downloads the CURRENT optimized resume variant's artifact -- distinct
    from /jobs/{job_id}/download/{file_key}, which serves the Phase 1
    pipeline's own (unoptimized) resume artifacts."""
    variant = resume_optimizer_repo.get_current_variant(job_id)
    if variant is None:
        raise HTTPException(404, "no optimized resume generated for this job yet")
    field = {"docx": "resume_docx_path", "pdf": "resume_pdf_path", "txt": "resume_txt_path"}.get(file_key)
    if not field:
        raise HTTPException(404, "unknown file type")
    path_str = variant.get(field)
    if not path_str:
        raise HTTPException(404, "file not generated for this variant")
    path = Path(path_str)
    if not path.exists():
        raise HTTPException(404, "file missing on disk")
    return FileResponse(path, filename=path.name)


@app.get("/api/jobs/{job_id}/jd-analysis")
def api_jd_analysis(job_id: int):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    fingerprint = compute_jd_fingerprint(job.title, job.company, job.description)
    analysis = resume_optimizer_repo.get_jd_analysis(job_id, fingerprint)
    if analysis is None:
        return JSONResponse({"analyzed": False, "jd_fingerprint": fingerprint})
    return JSONResponse({"analyzed": True, **analysis})


@app.get("/api/jobs/{job_id}/resume-quality")
def api_resume_quality(job_id: int):
    report = resume_optimizer_repo.get_quality_report_for_job(job_id)
    if report is None:
        raise HTTPException(404, "no resume quality report for this job -- generate a resume first")
    return JSONResponse(report["report"])


@app.get("/api/jobs/{job_id}/resume-evidence")
def api_resume_evidence(job_id: int):
    variant = resume_optimizer_repo.get_current_variant(job_id)
    if variant is None:
        raise HTTPException(404, "no resume variant for this job yet")
    return JSONResponse({
        "variant_id": variant["variant_id"], "status": variant["status"],
        "evidence_links": resume_optimizer_repo.list_evidence_links(variant["variant_id"]),
    })


@app.get("/api/pipeline/summary")
def api_pipeline_summary():
    return JSONResponse(pipeline_dashboard.compute_pipeline_summary(list_jobs({})))


@app.get("/resume-optimizer/doctor", response_class=HTMLResponse)
def resume_optimizer_doctor_page(request: Request):
    report = resume_optimizer_doctor.run_doctor()
    return templates.TemplateResponse(request, "resume_optimizer_doctor.html", {"report": report})


@app.get("/api/resume-optimizer/metrics")
def api_resume_optimizer_metrics():
    return JSONResponse(resume_optimizer_metrics.collect())


# --- Phase 8: safe ATS application executor ---------------------------------

_APPLICATIONS_TABS = [
    ("", "All"), ("ready", "Ready to Apply"), ("approved", "Ready for Final Review"), ("needs_action", "Needs Action"),
    ("submitting", "Applying"), ("applied", "Applied"), ("completed_by_user", "Completed by You"),
    ("failed", "Failed"), ("skipped", "Skipped"),
]


@app.get("/applications", response_class=HTMLResponse)
def applications_page(
    request: Request, bucket: str = "", company: str = "", provider: str = "",
    work_arrangement: str = "", sponsorship_status: str = "",
):
    if bucket == "skipped":
        rows = pipeline_dashboard.list_skipped_job_rows(
            company=company, provider=provider, work_arrangement=work_arrangement,
            sponsorship_status=sponsorship_status,
        )
    else:
        rows = applications_repo.list_executions_with_jobs(
            bucket=bucket, company=company, provider=provider,
            work_arrangement=work_arrangement, sponsorship_status=sponsorship_status, limit=200,
        )
    tab_counts = applications_repo.bucket_counts()
    tab_counts["skipped"] = pipeline_dashboard.count_skipped_jobs()
    # "All" must equal the true total of every execution status exactly
    # once -- summed over the non-overlapping leaf buckets (never the
    # "in_flight" convenience union, which would double-count against
    # "ready"/"submitting" etc).
    tab_counts[""] = sum(
        tab_counts.get(b, 0) for b in (
            "ready", "approved", "queued", "preparing", "submitting", "needs_action", "applied",
            "completed_by_user", "failed",
        )
    ) + tab_counts["skipped"]

    job_ids = [r["job_id"] for r in rows if r.get("job_id") is not None]
    session_by_job = browser_session.get_active_sessions_for_jobs(job_ids)
    for r in rows:
        if bucket == "skipped":
            r["cta"] = compute_apply_cta(r["job_id"], r["status"]).as_dict()
        else:
            r["cta"] = compute_apply_cta(
                r["job_id"], None, execution=r, browser_session=session_by_job.get(r["job_id"]),
            ).as_dict()

    return templates.TemplateResponse(
        request, "applications.html",
        {
            **_nav_ctx(),
            "rows": rows, "filters": {
                "bucket": bucket, "company": company, "provider": provider,
                "work_arrangement": work_arrangement, "sponsorship_status": sponsorship_status,
            },
            "tabs": _APPLICATIONS_TABS,
            "tab_counts": tab_counts,
            "buckets": list(applications_repo.DASHBOARD_BUCKETS.keys()),
            "executor_enabled": config.APPLICATION_EXECUTOR_ENABLED,
            "auto_submit_enabled": config.AUTO_SUBMIT_ENABLED,
            "auto_prepare_enabled": config.APPLICATION_AUTO_PREPARE_ENABLED,
            "metrics": applications_metrics.collect(),
            "fleet": applications_metrics.collect_worker_fleet(),
            "budget": applications_budget.collect().as_dict(),
            "browser_assist_enabled": config.BROWSER_ASSIST_ENABLED,
            "browser_headless": config.BROWSER_HEADLESS,
        },
    )


# --- Phase 9: production application-worker fleet ---------------------------

@app.get("/application-workers", response_class=HTMLResponse)
def application_workers_page(request: Request):
    all_workers = workers_repo.list_workers(limit=200)
    app_workers = [
        w for w in all_workers
        if application_worker_has_capability(w.get("capabilities") or "[]", WorkerCapability.APPLICATION_PREPARE)
        or application_worker_has_capability(w.get("capabilities") or "[]", WorkerCapability.APPLICATION_SUBMIT)
    ]
    circuit_rows = [{"provider": p, "state": s} for p, s in applications_circuit.all_states().items()]
    return templates.TemplateResponse(
        request, "application_workers.html",
        {
            "workers": app_workers,
            "attempts": applications_attempts.list_recent_attempts(limit=100),
            "fleet": applications_metrics.collect_worker_fleet(),
            "budget": applications_budget.collect().as_dict(),
            "circuit_rows": circuit_rows,
            "executor_enabled": config.APPLICATION_EXECUTOR_ENABLED,
            "auto_submit_enabled": config.AUTO_SUBMIT_ENABLED,
            "auto_prepare_enabled": config.APPLICATION_AUTO_PREPARE_ENABLED,
            "config": {
                "application_worker_concurrency": config.APPLICATION_WORKER_CONCURRENCY,
                "application_lease_seconds": config.APPLICATION_LEASE_SECONDS,
                "application_provider_concurrency_default": config.APPLICATION_PROVIDER_CONCURRENCY_DEFAULT,
                "application_circuit_breaker_cooldown_seconds": config.APPLICATION_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
            },
        },
    )


@app.post("/application-workers/{worker_id}/drain")
def application_worker_drain(worker_id: str):
    application_request_drain(worker_id)
    return RedirectResponse(url="/application-workers", status_code=303)


@app.post("/application-workers/{worker_id}/resume-drain")
def application_worker_resume_drain(worker_id: str):
    application_resume_from_drain(worker_id)
    return RedirectResponse(url="/application-workers", status_code=303)


@app.post("/application-workers/{worker_id}/mark-offline")
def application_worker_mark_offline(worker_id: str):
    workers_repo.mark_worker_offline(worker_id)
    return RedirectResponse(url="/application-workers", status_code=303)


@app.post("/applications/circuit/{provider}/force-probe")
def application_circuit_force_probe(provider: str):
    applications_circuit.force_probe(provider)
    return RedirectResponse(url="/application-workers", status_code=303)


@app.post("/applications/circuit/{provider}/close")
def application_circuit_close(provider: str):
    applications_circuit.force_close(provider)
    return RedirectResponse(url="/application-workers", status_code=303)


@app.post("/applications/scheduler/run")
def application_scheduler_run_once():
    result = applications_scheduler.run_cycle()
    return JSONResponse(result.as_dict())


@app.post("/applications/reconcile-worker/run")
def application_reconcile_worker_run_once():
    result = applications_reconcile_worker.run_pass()
    return JSONResponse(result.as_dict())


@app.get("/applications/capability-matrix", response_class=HTMLResponse)
def application_capability_matrix_page(request: Request):
    matrix = applications_capability_matrix.build_matrix()
    return templates.TemplateResponse(request, "application_capability_matrix.html", {"matrix": matrix})


@app.get("/applications/execution-contract", response_class=HTMLResponse)
def provider_execution_contract_page(request: Request):
    """Real Provider Execution V1: the unified seven-flag provider EXECUTION
    contract (app.applications.execution_contract). Read-only and wholly
    derived -- it owns no capability facts of its own, so it can never
    inflate one; it exists so a reader can see form-discovery/fill/upload/
    assist capability and SUBMISSION capability side by side and confirm
    they are genuinely separate columns."""
    from app.applications import execution_contract

    matrix = execution_contract.build_matrix()
    return templates.TemplateResponse(request, "provider_execution_contract.html", {"matrix": matrix})


@app.get("/api/applications/execution-contract")
def provider_execution_contract_api():
    from app.applications import execution_contract

    return {"providers": [c.as_dict() for c in execution_contract.all_contracts()]}


# --- Application-lifecycle-exception-resume-v1: consumer board/detail -------

@app.get("/applications/board", response_class=HTMLResponse)
def applications_board_page(request: Request):
    buckets = applications_board.build_board()
    return templates.TemplateResponse(
        request, "applications_board.html",
        {**_nav_ctx(), "buckets": buckets, "bucket_labels": applications_board.BUCKET_LABELS,
         "bucket_order": [applications_board.BUCKET_NEEDS_ACTION, applications_board.BUCKET_READY_TO_APPLY,
                          applications_board.BUCKET_IN_PROGRESS, applications_board.BUCKET_SUBMITTED,
                          applications_board.BUCKET_ISSUES],
         "counts": applications_board.bucket_counts(buckets)},
    )


@app.get("/applications/{execution_id}/detail", response_class=HTMLResponse)
def application_detail_page(request: Request, execution_id: str):
    """One-click-application-experience-v1 section C: the full tabbed
    Overview / Job / Resume / Cover Letter / Answers / Timeline / Issues /
    Receipt experience for one application. Every piece of context here is
    read from an already-existing, unmodified module (never a second
    computation of stage/CTA/quality/eligibility) -- this route only
    assembles them for one template."""
    execution = applications_repo.get_execution(execution_id)
    if execution is None:
        raise HTTPException(404, "application execution not found")
    job = get_job(execution["job_id"])
    if job is None:
        raise HTTPException(404, "job not found")

    from app.applications import receipts as applications_receipts

    answers = applications_repo.list_answer_snapshot(execution_id)
    blocker_history = applications_blockers.list_blockers_for_execution(execution_id)
    audit_log = applications_repo.list_audit_log(execution_id=execution_id)
    receipt = applications_receipts.get_latest_receipt_for_execution(execution_id)
    approval_freshness = applications_approval.check_approval_freshness(job.id)
    active_session = browser_session.get_active_session_for_job(job.id)
    eligibility = evaluate_executor_eligibility(job)
    cta = compute_apply_cta(
        job.id, job.application_state.value, execution=execution, browser_session=active_session,
    )

    # Section E (Resume Review) / F (Approval) diagnostics -- the SAME
    # resume_optimizer/JD-analysis read layer job_detail() already uses,
    # never a second computation.
    current_variant = resume_optimizer_repo.get_current_variant(job.id)
    if current_variant is not None and isinstance(current_variant.get("compression_log"), str):
        try:
            current_variant["compression_log"] = json.loads(current_variant["compression_log"] or "[]")
        except json.JSONDecodeError:
            current_variant["compression_log"] = []
    quality_row = resume_optimizer_repo.get_quality_report_for_job(job.id)
    jd_fingerprint = compute_jd_fingerprint(job.title, job.company, job.description)
    jd_analysis_row = resume_optimizer_repo.get_jd_analysis(job.id, jd_fingerprint)

    submission_supported = None
    try:
        from app.applications.provider_registry import get_application_provider

        submission_supported = get_application_provider(job).capabilities.submission_supported
    except Exception:  # noqa: BLE001 -- diagnostics-only; never break the page over a provider lookup issue
        submission_supported = None

    # Timeline: audit-log events and blocker raise/resolve events merged by
    # time -- never fabricated, both are genuine durable records.
    timeline: list[dict] = []
    for e in audit_log:
        timeline.append({"at": e["created_at"], "kind": "event", "label": e["event_type"], "detail": e["detail"]})
    for b in blocker_history:
        timeline.append({"at": b["created_at"], "kind": "blocker_raised", "label": b["human_title"],
                          "detail": b["human_message"]})
        if b["resolved_at"]:
            timeline.append({"at": b["resolved_at"], "kind": "blocker_resolved", "label": f"Resolved: {b['human_title']}",
                              "detail": b.get("resolution_note") or ""})
    timeline.sort(key=lambda t: t["at"])

    active_blocker = applications_blockers.get_active_blocker_for_execution(execution_id)
    stage_info = compute_stage(execution)

    # Final Review tab (daily-use-v1): the SAME read-only, no-new-gate
    # presubmit manifest already exposed via `python -m app.applications.cli
    # presubmit-manifest` -- this route only renders it, never a second
    # readiness computation. discover_form=False avoids a real network read
    # on every ordinary page load; the CLI remains the way to force a fresh
    # provider-form check when actually needed.
    final_review = presubmit_manifest.build_manifest(job.id, discover_form=False)
    ready_for_final_review = applications_handoff.is_ready_for_final_review(execution)
    recruiter_updates = applications_recruiter.list_updates(job_id=job.id)
    mailbox_status = applications_recruiter.mailbox_status()

    return templates.TemplateResponse(
        request, "application_detail.html",
        {
            **_nav_ctx(), "job": job, "execution": execution, "answers": answers, "timeline": timeline,
            "receipt": receipt, "active_blocker": active_blocker, "blocker_history": blocker_history,
            "approval_freshness": approval_freshness, "stage_label": stage_info.label,
            "cta": cta.as_dict(), "eligibility": eligibility, "submission_supported": submission_supported,
            "current_variant": current_variant, "quality_report": quality_row["report"] if quality_row else None,
            "jd_analysis": jd_analysis_row,
            "final_review": final_review.as_dict() if final_review else None,
            "ready_for_final_review": ready_for_final_review,
            "recruiter_updates": recruiter_updates,
            "mailbox_status": mailbox_status,
            "auto_submit_enabled": config.AUTO_SUBMIT_ENABLED,
            # Advanced/debug section (section C): technical identifiers only
            # -- never a raw lifecycle-state enum (execution.status/
            # automation_policy/policy_reasons), which every OTHER section of
            # this page already renders as plain language (stage_label,
            # active_blocker.human_title, cta.label) instead.
            "execution_json": json.dumps(
                {
                    k: execution.get(k) for k in (
                        "execution_id", "job_id", "mode", "attempt_count", "started_at", "finished_at",
                        "updated_at", "resume_artifact_hash", "answers_version", "submission_method",
                        "correlation_id", "requires_user_action",
                    )
                },
                indent=2, default=str,
            ),
        },
    )


# --- Application-lifecycle-exception-resume-v1: Demo / Test Mode -----------

@app.get("/demo", response_class=HTMLResponse)
def demo_page(request: Request):
    return templates.TemplateResponse(
        request, "demo.html", {**_nav_ctx(), "scenarios": applications_demo.list_demo_status()},
    )


@app.post("/demo/run-all")
def demo_run_all():
    """One-click-application-experience-v1 section L: a single action that
    processes the entire local demo fixture set (prepared/completed, needs
    action, skipped, transient-recovered, submission-ambiguous) without any
    manual per-scenario babysitting."""
    applications_demo.run_all_demos()
    return RedirectResponse(url="/demo", status_code=303)


@app.post("/demo/{scenario_key}/run")
def demo_run(scenario_key: str):
    try:
        applications_demo.run_demo(scenario_key)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return RedirectResponse(url="/demo", status_code=303)


@app.post("/demo/{scenario_key}/resolve")
def demo_resolve(scenario_key: str):
    try:
        applications_demo.resolve_demo(scenario_key)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return RedirectResponse(url="/demo", status_code=303)


@app.get("/api/applications/budget")
def api_applications_budget():
    return JSONResponse(applications_budget.collect().as_dict())


@app.get("/applications/doctor", response_class=HTMLResponse)
def applications_doctor_page(request: Request):
    report = applications_doctor.run_doctor()
    return templates.TemplateResponse(request, "applications_doctor.html", {"report": report})


@app.get("/api/applications/metrics")
def api_applications_metrics():
    return JSONResponse(applications_metrics.collect())


@app.get("/api/jobs/{job_id}/eligibility")
def api_job_eligibility(job_id: int):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return JSONResponse(evaluate_executor_eligibility(job).as_dict())


@app.get("/api/executions/{execution_id}")
def api_execution_detail(execution_id: str):
    execution = applications_repo.get_execution(execution_id)
    if execution is None:
        raise HTTPException(404, "execution not found")
    return JSONResponse({
        "execution": execution,
        "answers": applications_repo.list_answer_snapshot(execution_id),
        "audit_log": applications_repo.list_audit_log(execution_id=execution_id),
    })


@app.post("/jobs/{job_id}/applications/queue")
def application_queue(job_id: int, mode: str = Form("ASSIST")):
    try:
        result = queue_application(job_id, mode=mode)
    except (ExecutorDisabledError, AutoSubmitDisabledError) as exc:
        raise HTTPException(400, str(exc))
    if not result.queued:
        raise HTTPException(400, f"not queued: {result.reason}")
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/applications/prepare")
def application_prepare(job_id: int, mode: str = Form("ASSIST")):
    """Queue + synchronously run one pass of the executor -- CLAUDE.md Phase
    8 section 43 "Prepare Application". Never bypasses any gate; identical
    logic to `python -m app.applications.cli prepare`."""
    try:
        result = queue_application(job_id, mode=mode)
    except (ExecutorDisabledError, AutoSubmitDisabledError) as exc:
        raise HTTPException(400, str(exc))
    if result.queued and result.execution_id:
        process_execution(result.execution_id)
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/applications/retry")
def application_retry(job_id: int):
    """CLAUDE.md Phase 8 section 43 "Retry Preparation"/"Mark User Action
    Completed" -- re-runs the executor pipeline for the job's existing
    active execution (e.g. after the candidate updated their profile to
    resolve a NEEDS_USER_ACTION gap). Never creates a second execution row
    while one is still active."""
    execution = applications_repo.get_active_execution_for_job(job_id)
    if execution is None:
        raise HTTPException(400, "no active execution for this job -- use Prepare Application first")
    process_execution(execution["execution_id"])
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/applications/approve")
def application_approve(job_id: int, request: Request):
    """APPROVE & APPLY (approval-gated-autonomy-v1 spec section 6) -- the
    ONE normal human gate this feature exists to implement. Records a
    durable, fingerprint-bound approval, then synchronously continues the
    application exactly as far as a genuinely verified provider capability
    (and every other existing safety gate) allows -- never a UI redirect
    that merely implies approval happened.

    application-action-experience-v1: when called via the JS-enhanced CTA
    button (Accept: application/json), returns the resulting CTA/execution
    as JSON instead of redirecting, so the button can update in place
    (disable -> "Applying..." -> live result) without a full navigation.
    The plain-form fallback (JS disabled) is unchanged."""
    wants_json = "application/json" in (request.headers.get("accept") or "")
    result = applications_approval.approve_and_apply(job_id)
    if not result.ok:
        if wants_json:
            return JSONResponse({"ok": False, "reason": result.reason}, status_code=400)
        raise HTTPException(400, result.reason)
    if wants_json:
        job = get_job(job_id)
        session = browser_session.get_active_session_for_job(job_id) if job else None
        cta = compute_apply_cta(
            job_id, job.application_state.value if job else None,
            execution=result.execution, browser_session=session,
        )
        return JSONResponse({"ok": True, "execution": result.execution, "cta": cta.as_dict()})
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.get("/api/jobs/{job_id}/apply-status")
def api_apply_status(job_id: int):
    """Polling endpoint for the Apply CTA (application-action-experience-v1
    section 12/6): the current authoritative CTA for one job, used by the
    JS-enhanced APPROVE & APPLY button to live-update after a click without
    a full page reload, and safe to poll from any page showing that job's
    CTA. Never mutates anything -- read-only, same as every other /api/*
    endpoint in this file."""
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    execution = applications_repo.get_active_execution_for_job(job_id)
    session = browser_session.get_active_session_for_job(job_id)
    cta = compute_apply_cta(job_id, job.application_state.value, execution=execution, browser_session=session)
    return JSONResponse({
        "cta": cta.as_dict(), "execution": execution, "application_state": job.application_state.value,
    })


@app.post("/applications/approve-bulk")
def application_approve_bulk(job_ids: str = Form(...)):
    """APPROVE & APPLY SELECTED (spec section 14): each job id in the
    comma-separated `job_ids` field (posted from the dashboard's bulk
    confirmation modal, which lists every company/role/sponsorship
    status/resume version/submission capability before this request is
    ever sent) gets its own individually-recorded approval; one job
    failing never stops the rest."""
    ids = [int(x) for x in job_ids.split(",") if x.strip().isdigit()]
    result = applications_approval.approve_and_apply_bulk(ids)
    return JSONResponse(result.as_dict())


@app.get("/api/jobs/{job_id}/approval-freshness")
def api_approval_freshness(job_id: int):
    return JSONResponse(applications_approval.check_approval_freshness(job_id))


@app.get("/api/ready-for-approval")
def api_ready_for_approval(limit: int = 50):
    return JSONResponse({
        "count": pipeline_dashboard.count_ready_for_approval(),
        "items": pipeline_dashboard.build_ready_for_approval_queue(limit=limit),
    })


@app.post("/executions/{execution_id}/reconcile")
def execution_reconcile(
    execution_id: str, resolution: str = Form(...), confirmation_id: str = Form(""), note: str = Form(""),
):
    execution = applications_repo.get_execution(execution_id)
    if execution is None:
        raise HTTPException(404, "execution not found")
    result = reconcile_execution(execution_id, resolution, confirmation_id=confirmation_id, note=note)
    if not result.ok:
        raise HTTPException(400, result.detail)
    return RedirectResponse(url=f"/jobs/{execution['job_id']}", status_code=303)


@app.post("/executions/{execution_id}/handoff-outcome")
def execution_handoff_outcome(
    execution_id: str, outcome: str = Form(...), confirmation_id: str = Form(""),
    confirmation_url: str = Form(""), note: str = Form(""),
):
    """Tsenta-parity-closure-v1, P0#2: the one action a human takes after
    using "Open Application / Continue Manually" from the READY FOR FINAL
    REVIEW hand-off, to tell the app what happened. See
    app.applications.handoff.record_manual_outcome for the full contract --
    never fabricates a receipt/confirmation, never marks APPLIED without a
    confirmation id/URL the human supplies themselves."""
    execution = applications_repo.get_execution(execution_id)
    if execution is None:
        raise HTTPException(404, "execution not found")
    result = applications_handoff.record_manual_outcome(
        execution_id, outcome, confirmation_id=confirmation_id, confirmation_url=confirmation_url, note=note,
    )
    if not result.ok:
        raise HTTPException(400, result.detail)
    return RedirectResponse(url=f"/applications/{execution_id}/detail#detail-final-review", status_code=303)


@app.post("/jobs/{job_id}/recruiter-updates")
def record_recruiter_update(
    job_id: int, update_type: str = Form(...), subject: str = Form(""), detail: str = Form(""),
    raise_needs_you: bool = Form(False),
):
    """Tsenta Remaining-Gaps Closure V2, section 6: manual recording of a
    recruiter/ATS communication update (no mailbox is connected -- see
    app.applications.recruiter_communication.NullMailboxAdapter). Never
    marks anything APPLIED on its own."""
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    execution = applications_repo.get_active_execution_for_job(job_id)
    result = applications_recruiter.record_update(
        job_id, update_type, execution_id=(execution["execution_id"] if execution else ""),
        subject=subject, detail=detail, raise_needs_you=raise_needs_you,
    )
    if not result.ok:
        raise HTTPException(400, result.detail)
    if execution:
        return RedirectResponse(
            url=f"/applications/{execution['execution_id']}/detail#detail-post-application", status_code=303,
        )
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@app.get("/applications/browser-sessions", response_class=HTMLResponse)
def browser_sessions_page(request: Request, status: str = ""):
    from app.applications.browser_session import BrowserSessionStatus

    rows = browser_session.list_sessions(status=status or None, limit=200)
    return templates.TemplateResponse(
        request, "browser_sessions.html",
        {
            "rows": rows, "status_filter": status,
            "statuses": [s.value for s in BrowserSessionStatus],
            "summary": browser_session.summarize().as_dict(),
            "metrics": applications_metrics.collect_browser_assist(),
            "browser_assist_enabled": config.BROWSER_ASSIST_ENABLED,
            "browser_headless": config.BROWSER_HEADLESS,
            "browser_concurrency": config.BROWSER_ASSIST_CONCURRENCY,
        },
    )


@app.get("/applications/browser-sessions/{session_id}", response_class=HTMLResponse)
def browser_session_detail_page(request: Request, session_id: str):
    session = browser_session.get_session(session_id)
    if session is None:
        raise HTTPException(404, "browser-assist session not found")
    job = get_job(session["job_id"])
    return templates.TemplateResponse(
        request, "browser_session_detail.html",
        {
            "session": session, "job": job, "is_live": applications_browser_assist.browser_runtime.is_live(session_id),
            "browser_assist_enabled": config.BROWSER_ASSIST_ENABLED,
        },
    )


@app.post("/jobs/{job_id}/browser-assist/start")
def browser_assist_start(job_id: int):
    active = applications_repo.get_active_execution_for_job(job_id)
    if active is None:
        raise HTTPException(400, "no active execution for this job -- use Prepare Application or Queue Application first")
    result = applications_browser_assist.start_session(active["execution_id"])
    if not result.get("created"):
        raise HTTPException(400, f"browser-assist session not created: {result.get('reason')}")
    session = result.get("session") or {}
    return RedirectResponse(url=f"/applications/browser-sessions/{session.get('session_id')}", status_code=303)


@app.post("/browser-sessions/{session_id}/resume")
def browser_session_resume(session_id: str):
    result = applications_browser_assist.resume_session(session_id)
    if not result.get("ok"):
        raise HTTPException(400, result.get("detail", "could not resume session"))
    return RedirectResponse(url=f"/applications/browser-sessions/{session_id}", status_code=303)


@app.post("/browser-sessions/{session_id}/continue")
def browser_session_continue(session_id: str):
    result = applications_browser_assist.mark_user_action_complete(session_id)
    if not result.get("ok"):
        raise HTTPException(400, result.get("detail", "could not continue session"))
    return RedirectResponse(url=f"/applications/browser-sessions/{session_id}", status_code=303)


@app.post("/browser-sessions/{session_id}/advance-step")
def browser_session_advance_step(session_id: str):
    result = applications_browser_assist.advance_step(session_id)
    if not result.get("ok"):
        raise HTTPException(400, result.get("detail", "could not advance to the next step"))
    return RedirectResponse(url=f"/applications/browser-sessions/{session_id}", status_code=303)


@app.post("/browser-sessions/{session_id}/close")
def browser_session_close(session_id: str, reason: str = Form("closed by user")):
    applications_browser_assist.close_session(session_id, reason=reason)
    return RedirectResponse(url="/applications/browser-sessions", status_code=303)


@app.post("/browser-sessions/{session_id}/reconcile")
def browser_session_reconcile(session_id: str):
    result = applications_browser_assist.attempt_user_submit_reconciliation(session_id)
    if not result.get("ok"):
        raise HTTPException(400, result.get("detail", "no confirmation evidence found"))
    return RedirectResponse(url=f"/applications/browser-sessions/{session_id}", status_code=303)


@app.post("/applications/browser-sessions/expire-stale")
def browser_sessions_expire_stale():
    expired = applications_browser_assist.expire_stale_sessions()
    return JSONResponse({"expired": len(expired)})


@app.get("/api/applications/browser-assist/metrics")
def api_browser_assist_metrics():
    metrics = applications_metrics.collect_browser_assist()
    metrics.update(applications_metrics.collect_phase11())
    metrics.update(applications_metrics.collect_phase12())
    metrics.update(applications_metrics.collect_phase13())
    return JSONResponse(metrics)


@app.get("/applications/browser-capability-matrix", response_class=HTMLResponse)
def browser_capability_matrix_page(request: Request):
    from app.applications import browser_capability_matrix

    matrix = browser_capability_matrix.build_matrix()
    return templates.TemplateResponse(request, "browser_capability_matrix.html", {"matrix": matrix})


@app.get("/applications/workday-tenants", response_class=HTMLResponse)
def workday_tenant_matrix_page(request: Request):
    """CLAUDE.md Phase 11 sections 45, 57: per-tenant/site Workday
    observations -- never a single collapsed 'Workday supported' claim."""
    from app.applications import workday_tenant

    return templates.TemplateResponse(
        request, "workday_tenants.html",
        {"observations": workday_tenant.list_observations(), "stability": workday_tenant.stability_report()},
    )


@app.get("/applications/capability-evidence", response_class=HTMLResponse)
def capability_evidence_page(request: Request):
    """CLAUDE.md Phase 11 sections 42-43, 57: dated capability evidence with
    staleness -- read-only, never auto-disables anything."""
    from app.applications import capability_evidence

    rows = capability_evidence.list_evidence()
    for row in rows:
        row["stale"] = capability_evidence.is_stale(row)
        row["age_days"] = round(capability_evidence.evidence_age_days(row["observed_at"]), 1)
    return templates.TemplateResponse(
        request, "capability_evidence.html",
        {"rows": rows, "max_age_days": config.CAPABILITY_EVIDENCE_MAX_AGE_DAYS},
    )


@app.get("/applications/provider-health", response_class=HTMLResponse)
def provider_health_page(request: Request):
    """CLAUDE.md Phase 13 sections 11-12, 57: real-browser ASSIST flow health
    per (provider, tenant, site) -- read-only, never per-provider collapsed."""
    from app.applications import provider_health

    return templates.TemplateResponse(request, "provider_health.html", {"rows": provider_health.list_health()})


@app.get("/api/applications/provider-health")
def api_provider_health():
    from app.applications import provider_health

    return JSONResponse(provider_health.list_health())


@app.get("/applications/receipts", response_class=HTMLResponse)
def application_receipts_page(request: Request, provider: str = ""):
    """Provider Post-Approval Execution V1: durable submission-evidence
    records, one row per genuinely confirmed APPLIED execution -- see
    app.applications.receipts's module docstring."""
    from app.applications import receipts as applications_receipts

    return templates.TemplateResponse(
        request, "application_receipts.html", {"rows": applications_receipts.list_receipts(provider=provider)},
    )


@app.get("/api/applications/receipts")
def api_application_receipts(provider: str = "", limit: int = 200):
    from app.applications import receipts as applications_receipts

    return JSONResponse(applications_receipts.list_receipts(provider=provider, limit=limit))


@app.get("/api/applications/job-identity")
def api_job_identity(job_id: int | None = None):
    from app.applications import job_identity

    return JSONResponse(job_identity.list_verifications(job_id=job_id))


@app.get("/api/applications/canary-runs")
def api_canary_runs(provider: str = ""):
    from app.applications import canary

    return JSONResponse(canary.list_canary_runs(provider=provider))


@app.get("/jobs/{job_id}/download/{file_key}")
def download_file(job_id: int, file_key: str):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    field = FILE_FIELDS.get(file_key)
    if not field:
        raise HTTPException(404, "unknown file type")
    path_str = getattr(job, field)
    if not path_str:
        raise HTTPException(404, "file not generated for this job")
    path = Path(path_str)
    if not path.exists():
        raise HTTPException(404, "file missing on disk")
    return FileResponse(path, filename=path.name)


@app.get("/agent/status")
def agent_status():
    status = agent_state.get_status()
    status["config"] = {
        "interval_minutes": config.DISCOVERY_INTERVAL_MINUTES,
        "max_jobs_per_cycle": config.MAX_JOBS_PER_CYCLE,
        "min_match_score": config.MIN_MATCH_SCORE,
        "freshness_max_days": config.FRESHNESS_MAX_DAYS,
        "enabled_providers": config.ENABLED_PROVIDERS,
    }
    status["recent_cycles"] = list_discovery_cycles(limit=10)
    status["orchestrator"] = agent_orchestrator.status()
    return JSONResponse(status)


@app.post("/agent/toggle")
def agent_toggle(enabled: bool = Form(...)):
    """Legacy Phase 2 discovery-only toggle -- kept working unchanged for any
    existing caller/deployment that still uses it. The primary control
    surface for the one-click agent is /agent/start and /agent/stop below,
    which additionally coordinate resume optimization and application
    preparation/execution."""
    agent_state.set_enabled(enabled)
    return RedirectResponse(url="/", status_code=303)


@app.post("/agent/start")
async def agent_start(test_mode: bool = Form(False)):
    """START AGENT (CLAUDE.md one-click-agent sections 1, 17, 43): returns
    quickly -- the actual discovery/resume/application work happens in the
    background orchestrator loop, never inside this request. No GET request
    may start the agent (mutating action, POST-only, matching every other
    mutating route in this project). Declared `async def` (unlike most
    routes in this file) so AgentOrchestrator.start()'s asyncio.create_task()
    call runs on the actual event loop thread -- a sync route handler runs
    in FastAPI's worker threadpool instead, where there is no running loop."""
    agent_orchestrator.start(test_mode=test_mode)
    return RedirectResponse(url="/", status_code=303)


@app.post("/agent/stop")
async def agent_stop():
    """STOP AGENT: finishes/releases current safe work, then stops cleanly
    -- never an abrupt interruption of a possible in-flight submission (see
    AgentOrchestrator.stop())."""
    await agent_orchestrator.stop()
    return RedirectResponse(url="/", status_code=303)


@app.get("/candidate/status")
def candidate_status():
    profile = load_profile()
    missing = missing_fields(profile)
    return {"missing_fields": missing, "missing_count": len(missing)}


@app.get("/health")
def health():
    """Liveness only -- process is up and can respond. Deliberately does NOT
    touch the database (CLAUDE.md Phase 6 section 31): a health check that
    depends on the DB can't distinguish 'this process is stuck' from 'the
    shared database is briefly slow', which is exactly what /readiness is
    for instead."""
    return {"status": "ok"}


@app.get("/readiness")
def readiness():
    """Database reachable + schema compatible (CLAUDE.md Phase 6 section
    31). In Postgres mode this genuinely fails when the shared DB is
    unavailable -- unlike /health, which never touches it. Never leaks DB
    credentials in the response."""
    result = check_readiness()
    status_code = 200 if result.ready else 503
    return JSONResponse(
        {
            "ready": result.ready,
            "database_backend": result.database_backend,
            "database_reachable": result.database_reachable,
            "schema_version": result.schema_version,
            "expected_schema_version": migrations.CURRENT_SCHEMA_VERSION,
            "detail": result.detail,
        },
        status_code=status_code,
    )


@app.get("/version")
def version_endpoint():
    """Release/build metadata (CLAUDE.md Phase 15 sections 17-18). No host
    secrets, no DSN, no DATABASE_URL -- app.version.release_info() only ever
    assembles identifiers already computed from source (schema version,
    optimizer/classifier versions, provider capability fingerprint)."""
    from app.version import release_info

    return release_info()


@app.get("/metrics")
def metrics_endpoint():
    """Prometheus text-exposition format (CLAUDE.md Phase 6 sections 29-30).
    Every value is a live DB query at scrape time -- see
    app/observability/metrics.py's module docstring for why this doesn't use
    prometheus_client. Never exposes candidate PII."""
    if not config.METRICS_ENABLED:
        raise HTTPException(404, "metrics endpoint disabled (METRICS_ENABLED=false)")
    body = observability_metrics.render_prometheus_text(observability_metrics.collect())
    return HTMLResponse(content=body, media_type="text/plain; version=0.0.4")


# --- Phase 5: fleet operations dashboard ------------------------------------

@app.get("/fleet", response_class=HTMLResponse)
def fleet_page(request: Request):
    workers = workers_repo.list_workers()
    stale = {w["worker_id"] for w in workers_repo.list_stale_workers(older_than_seconds=config.WORKER_HEARTBEAT_SECONDS * 4)}
    attempts = workers_repo.list_recent_attempts(limit=100)
    dead_letters = workers_dead_letter.list_dead_letters(limit=100)
    snapshot = workers_metrics.fleet_snapshot()
    latency = workers_metrics.discovery_latency_percentiles()
    readiness = check_readiness()
    circuit_rows = [
        {"provider": p, "state": s} for p, s in observability_metrics.provider_circuit_states().items()
    ]
    schema_drift = schema_drift_repo.list_recent_drift(limit=50)
    return templates.TemplateResponse(
        request, "fleet.html",
        {
            "workers": workers, "stale_worker_ids": stale, "attempts": attempts,
            "dead_letters": dead_letters, "snapshot": snapshot, "latency": latency,
            "active_poll_leases": workers_leasing.count_active_poll_leases(),
            "active_verification_leases": workers_leasing.count_active_verification_leases(),
            "circuit_rows": circuit_rows,
            "schema_drift": schema_drift,
            "system": {
                "database_backend": db_backend(),
                "schema_version": readiness.schema_version,
                "expected_schema_version": migrations.CURRENT_SCHEMA_VERSION,
                "queue_backend": "PostgreSQL (SKIP LOCKED)" if db_backend() == "postgres" else "SQLite (WAL + busy_timeout)",
                "worker_software_version": WORKER_SOFTWARE_VERSION,
            },
            "config": {
                "poll_worker_concurrency": config.POLL_WORKER_CONCURRENCY,
                "portal_lease_seconds": config.PORTAL_LEASE_SECONDS,
                "shard_count": config.REGISTRY_SHARD_COUNT,
                "shard_index": config.REGISTRY_SHARD_INDEX,
                "dead_letter_max_attempts": config.DEAD_LETTER_MAX_ATTEMPTS,
                "circuit_breaker_failure_threshold": config.CIRCUIT_BREAKER_FAILURE_THRESHOLD,
                "circuit_breaker_cooldown_seconds": config.CIRCUIT_BREAKER_COOLDOWN_SECONDS,
                "orphan_worker_stale_seconds": config.ORPHAN_WORKER_STALE_SECONDS,
            },
        },
    )


@app.post("/fleet/dead-letter/{dead_letter_id}/requeue")
def fleet_dead_letter_requeue(dead_letter_id: int):
    workers_dead_letter.requeue(dead_letter_id)
    return RedirectResponse(url="/fleet", status_code=303)


@app.get("/fleet/metrics")
def fleet_metrics():
    return JSONResponse({
        "snapshot": workers_metrics.fleet_snapshot(),
        "discovery_latency": workers_metrics.discovery_latency_percentiles(),
        "active_poll_leases": workers_leasing.count_active_poll_leases(),
        "active_verification_leases": workers_leasing.count_active_verification_leases(),
    })


# --- Phase 6 (CLAUDE.md section 34): fleet admin safety actions -------------
# All POST, all explicit operator actions, none destructive ("delete all"
# controls are never provided).

@app.post("/fleet/circuit/{provider}/force-probe")
def fleet_force_probe(provider: str):
    workers_circuit.force_probe(provider)
    return RedirectResponse(url="/fleet", status_code=303)


@app.post("/fleet/circuit/{provider}/close")
def fleet_close_circuit(provider: str):
    workers_circuit.force_close(provider)
    return RedirectResponse(url="/fleet", status_code=303)


@app.post("/fleet/workers/{worker_id}/mark-offline")
def fleet_mark_worker_offline(worker_id: str):
    workers_repo.mark_worker_offline(worker_id)
    return RedirectResponse(url="/fleet", status_code=303)


@app.post("/fleet/reap-orphans")
def fleet_reap_orphans():
    workers_reaper.reap_orphans(stale_after_seconds=config.ORPHAN_WORKER_STALE_SECONDS)
    return RedirectResponse(url="/fleet", status_code=303)


# --- Phase 5: registry acquisition dashboard --------------------------------

@app.get("/acquisition", response_class=HTMLResponse)
def acquisition_page(request: Request):
    batches = registry_acquisition.list_batches()
    return templates.TemplateResponse(request, "acquisition.html", {"batches": batches})


@app.post("/acquisition/batches/{batch_id}/resume")
def acquisition_resume(batch_id: int):
    batch = registry_acquisition.get_batch(batch_id)
    if batch is None:
        raise HTTPException(404, "batch not found")
    if batch["status"] not in ("FAILED", "PAUSED"):
        raise HTTPException(400, f"batch is {batch['status']} -- only FAILED/PAUSED batches can be resumed")
    registry_acquisition.run_acquisition_batch(batch["path"], resume_batch_id=batch_id)
    return RedirectResponse(url="/acquisition", status_code=303)
