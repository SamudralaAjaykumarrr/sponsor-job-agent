import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import config
from app.agent import state as agent_state
from app.agent.scheduler import scheduler
from app.applications.tracker import can_transition
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
from app.workers import dead_letter as workers_dead_letter
from app.workers import leasing as workers_leasing
from app.workers import metrics as workers_metrics
from app.workers import repo as workers_repo


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    if config.REGISTRY_SEED_DEMO_DATA:
        seed_demo_entries()
    scheduler.start()
    yield
    await scheduler.stop()


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
):
    filters = {
        "work_arrangement": work_arrangement or None,
        "sponsorship_status": sponsorship_status or None,
        "application_state": application_state or None,
        "fresh_under_1hr": fresh_under_1hr or None,
        "fresh_under_6hr": fresh_under_6hr or None,
        "high_priority": high_priority or None,
    }
    jobs = list_jobs(filters)
    missing = missing_fields(load_profile())
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "jobs": jobs, "filters": filters,
            "missing_profile_fields": missing[:10],
            "missing_profile_count": len(missing),
            "agent": agent_state.get_status(),
            "agent_config": {
                "interval_minutes": config.DISCOVERY_INTERVAL_MINUTES,
                "max_jobs_per_cycle": config.MAX_JOBS_PER_CYCLE,
                "min_match_score": config.MIN_MATCH_SCORE,
                "enabled_providers": config.ENABLED_PROVIDERS,
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
    return templates.TemplateResponse(
        request, "job_detail.html",
        {"job": job, "score_breakdown": score_breakdown, "history": history, "provenance": provenance},
    )


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
    return {"status": "ok"}


# --- Phase 5: fleet operations dashboard ------------------------------------

@app.get("/fleet", response_class=HTMLResponse)
def fleet_page(request: Request):
    workers = workers_repo.list_workers()
    stale = {w["worker_id"] for w in workers_repo.list_stale_workers(older_than_seconds=config.WORKER_HEARTBEAT_SECONDS * 4)}
    attempts = workers_repo.list_recent_attempts(limit=100)
    dead_letters = workers_dead_letter.list_dead_letters(limit=100)
    snapshot = workers_metrics.fleet_snapshot()
    latency = workers_metrics.discovery_latency_percentiles()
    return templates.TemplateResponse(
        request, "fleet.html",
        {
            "workers": workers, "stale_worker_ids": stale, "attempts": attempts,
            "dead_letters": dead_letters, "snapshot": snapshot, "latency": latency,
            "active_poll_leases": workers_leasing.count_active_poll_leases(),
            "active_verification_leases": workers_leasing.count_active_verification_leases(),
            "config": {
                "poll_worker_concurrency": config.POLL_WORKER_CONCURRENCY,
                "portal_lease_seconds": config.PORTAL_LEASE_SECONDS,
                "shard_count": config.REGISTRY_SHARD_COUNT,
                "shard_index": config.REGISTRY_SHARD_INDEX,
                "dead_letter_max_attempts": config.DEAD_LETTER_MAX_ATTEMPTS,
                "circuit_breaker_failure_threshold": config.CIRCUIT_BREAKER_FAILURE_THRESHOLD,
                "circuit_breaker_cooldown_seconds": config.CIRCUIT_BREAKER_COOLDOWN_SECONDS,
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
