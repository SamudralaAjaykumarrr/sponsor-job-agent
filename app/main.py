import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import config
from app.agent import state as agent_state
from app.agent.scheduler import scheduler
from app.applications import doctor as applications_doctor
from app.applications import metrics as applications_metrics
from app.applications import repo as applications_repo
from app.applications.eligibility import evaluate_executor_eligibility
from app.applications.executor import (
    AutoSubmitDisabledError,
    ExecutorDisabledError,
    process_execution,
    queue_application,
)
from app.applications.reconcile import reconcile_execution
from app.applications.tracker import can_transition
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
    scheduler.start()
    applications_background_scheduler.start()
    resume_optimization_scheduler.start()
    yield
    await scheduler.stop()
    await applications_background_scheduler.stop()
    await resume_optimization_scheduler.stop()


app = FastAPI(title="Sponsor Job Agent", lifespan=lifespan)
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

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
):
    """CLAUDE.md Phase 14 sections 44-55, 79: the unified one-page dashboard
    -- summary cards, the pipeline table (with JD-coverage/resume/
    application/user-action columns sourced from cached, indexed tables,
    never recomputed live), and filters, all on one screen. Specialist pages
    (/applications, /fleet, /registry, ...) remain for admin/debugging."""
    import app.pipeline_dashboard as pipeline_dashboard

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
    all_jobs = list_jobs({})
    summary = pipeline_dashboard.compute_pipeline_summary(all_jobs)

    jobs = list_jobs(filters)
    if full_time_only:
        jobs = [j for j in jobs if pipeline_dashboard.is_actionable(j)]

    job_ids = [j.id for j in jobs if j.id is not None]
    quality_by_job = resume_optimizer_repo.get_quality_reports_for_jobs(job_ids)
    variant_by_job = resume_optimizer_repo.get_current_variants_for_jobs(job_ids)
    active_execution_by_job = applications_repo.get_active_executions_for_jobs(job_ids)

    def resume_status_of(jid: int) -> str:
        variant = variant_by_job.get(jid)
        return variant["status"] if variant else "NOT_GENERATED"

    if resume_status:
        jobs = [j for j in jobs if resume_status_of(j.id) == resume_status]
    if needs_action_only:
        jobs = [j for j in jobs if (active_execution_by_job.get(j.id) or {}).get("requires_user_action")]

    # CLAUDE.md Phase 15 section 42/44: bound the rendered table to the
    # top-N matching jobs (already priority-sorted) rather than rendering
    # every match -- a large-state benchmark measured unbounded rendering
    # growing to tens of MB of HTML at high job counts. Applied last, after
    # every filter above, so resume_status/needs_action_only still search
    # the full matching set, never just the first page of it.
    total_matching = len(jobs)
    if total_matching > config.DASHBOARD_MAX_TABLE_ROWS:
        jobs = jobs[: config.DASHBOARD_MAX_TABLE_ROWS]

    pipeline_rows = [
        {
            "job": j,
            "quality_report": (quality_by_job.get(j.id) or {}).get("report"),
            "resume_status": resume_status_of(j.id),
            "execution": active_execution_by_job.get(j.id),
        }
        for j in jobs
    ]

    missing = missing_fields(load_profile())
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "jobs": jobs, "pipeline_rows": pipeline_rows, "filters": filters,
            "summary": summary,
            "total_matching": total_matching,
            "table_row_cap": config.DASHBOARD_MAX_TABLE_ROWS,
            "missing_profile_fields": missing[:10],
            "missing_profile_count": len(missing),
            "agent": agent_state.get_status(),
            "agent_config": {
                "interval_minutes": config.DISCOVERY_INTERVAL_MINUTES,
                "max_jobs_per_cycle": config.MAX_JOBS_PER_CYCLE,
                "min_match_score": config.MIN_MATCH_SCORE,
                "enabled_providers": config.ENABLED_PROVIDERS,
            },
            "resume_optimization_enabled": config.RESUME_OPTIMIZATION_ENABLED,
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
    eligibility = evaluate_executor_eligibility(job)
    active_browser_session = browser_session.get_active_session_for_job(job_id)

    # --- Phase 14: JD analysis / resume optimization diagnostics ------------
    current_variant = resume_optimizer_repo.get_current_variant(job_id)
    quality_row = resume_optimizer_repo.get_quality_report_for_job(job_id)
    jd_fingerprint = compute_jd_fingerprint(job.title, job.company, job.description)
    jd_analysis_row = resume_optimizer_repo.get_jd_analysis(job_id, jd_fingerprint)
    evidence_links = resume_optimizer_repo.list_evidence_links(current_variant["variant_id"]) if current_variant else []
    alignment_priority = compute_alignment_priority(job, quality_row["report"] if quality_row else None)

    return templates.TemplateResponse(
        request, "job_detail.html",
        {
            "job": job, "score_breakdown": score_breakdown, "history": history, "provenance": provenance,
            "latest_decision": latest_decision, "decision_history": decision_history,
            "executions": executions, "active_execution": active_execution, "eligibility": eligibility,
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
        },
    )


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
    import app.pipeline_dashboard as pipeline_dashboard

    return JSONResponse(pipeline_dashboard.compute_pipeline_summary(list_jobs({})))


@app.get("/resume-optimizer/doctor", response_class=HTMLResponse)
def resume_optimizer_doctor_page(request: Request):
    report = resume_optimizer_doctor.run_doctor()
    return templates.TemplateResponse(request, "resume_optimizer_doctor.html", {"report": report})


@app.get("/api/resume-optimizer/metrics")
def api_resume_optimizer_metrics():
    return JSONResponse(resume_optimizer_metrics.collect())


# --- Phase 8: safe ATS application executor ---------------------------------

@app.get("/applications", response_class=HTMLResponse)
def applications_page(
    request: Request, bucket: str = "", company: str = "", provider: str = "",
    work_arrangement: str = "", sponsorship_status: str = "",
):
    rows = applications_repo.list_executions_with_jobs(
        bucket=bucket, company=company, provider=provider,
        work_arrangement=work_arrangement, sponsorship_status=sponsorship_status, limit=200,
    )
    return templates.TemplateResponse(
        request, "applications.html",
        {
            "rows": rows, "filters": {
                "bucket": bucket, "company": company, "provider": provider,
                "work_arrangement": work_arrangement, "sponsorship_status": sponsorship_status,
            },
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
    return JSONResponse(status)


@app.post("/agent/toggle")
def agent_toggle(enabled: bool = Form(...)):
    agent_state.set_enabled(enabled)
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
