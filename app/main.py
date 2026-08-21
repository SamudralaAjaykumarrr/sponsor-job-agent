from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.applications.tracker import can_transition
from app.candidate.profile import load_profile, missing_fields
from app.config import BASE_DIR
from app.db import init_db
from app.jobs_repo import get_job, list_jobs
from app.models import ApplicationMode, ApplicationState, Job
from app.pipeline import ingest_and_process

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


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
def dashboard(request: Request, work_arrangement: str = "", sponsorship_status: str = "",
              application_state: str = "", fresh_under_1hr: bool = False, high_priority: bool = False):
    filters = {
        "work_arrangement": work_arrangement or None,
        "sponsorship_status": sponsorship_status or None,
        "application_state": application_state or None,
        "fresh_under_1hr": fresh_under_1hr or None,
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
        },
    )


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: int):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return templates.TemplateResponse(request, "job_detail.html", {"job": job})


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


@app.get("/candidate/status")
def candidate_status():
    profile = load_profile()
    missing = missing_fields(profile)
    return {"missing_fields": missing, "missing_count": len(missing)}


@app.get("/health")
def health():
    return {"status": "ok"}
