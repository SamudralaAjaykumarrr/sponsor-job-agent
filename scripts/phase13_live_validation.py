"""CLAUDE.md Phase 13 sections 74-77: bounded, read-only, real-public-ATS
canary validation for the Phase 13 resilience layer
(`app.applications.canary`). Reuses the EXACT same real, previously-
discovered public postings scripts/phase11_live_validation.py and
scripts/phase12_live_validation.py already vetted (their own API-discovery
calls, re-run here) -- never a guessed URL. At most ONE canary run per
provider per invocation of this script; Workday gets a small, bounded
number of repeated observations (matching Phase 12's own rule), never an
excessive reload loop.

A canary run here NEVER fills candidate PII, uploads a resume, or clicks a
final submit -- see `app.applications.canary`'s own docstring for the exact
boundaries. A CAPTCHA/anti-bot challenge encountered is always reported as
the honest result, never worked around.

Usage:
    python scripts/phase13_live_validation.py

Requires network access and a launchable Chromium. If either is
unavailable, each provider's section reports NOT RUN with the real reason
rather than fabricating a result."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app import config

config.BROWSER_ASSIST_ENABLED = True
config.BROWSER_HEADLESS = True
config.REAL_ATS_CANARY_ENABLED = True

from app.applications import canary  # noqa: E402
from app.applications.workday_tenant import classify_stability, parse_workday_tenant  # noqa: E402
from app.db import init_db  # noqa: E402

RESULTS: list[dict] = []


def _report(provider: str, company: str, **fields) -> None:
    row = {"provider": provider, "company": company, **fields}
    RESULTS.append(row)
    print(f"\n=== {provider} ({company}) ===")
    for k, v in fields.items():
        print(f"  {k}: {v}")


def _run_canary(provider: str, company: str, url: str) -> None:
    try:
        result = canary.run_and_record_canary(url, provider=provider)
        _report(provider, company, url=url, **{k: v for k, v in result.items() if k not in ("id", "url", "provider")})
    except canary.CanaryUnavailable as exc:
        _report(provider, company, result="NOT RUN", reason=str(exc))
    except Exception as exc:  # noqa: BLE001 -- one provider's failure never aborts the others
        _report(provider, company, result="NOT RUN", reason=f"{type(exc).__name__}: {exc}")


def validate_greenhouse() -> None:
    try:
        resp = httpx.get("https://boards-api.greenhouse.io/v1/boards/gitlab/jobs?content=false", timeout=10.0)
        resp.raise_for_status()
        jobs = resp.json().get("jobs", [])
        if not jobs:
            _report("greenhouse", "GitLab", result="NOT RUN", reason="no jobs returned")
            return
        _run_canary("greenhouse", "GitLab", jobs[0]["absolute_url"])
    except Exception as exc:  # noqa: BLE001
        _report("greenhouse", "GitLab", result="NOT RUN", reason=f"{type(exc).__name__}: {exc}")


def validate_lever() -> None:
    try:
        resp = httpx.get("https://api.lever.co/v0/postings/leverdemo", params={"mode": "json"}, timeout=10.0)
        resp.raise_for_status()
        postings = resp.json()
        if not postings:
            _report("lever", "Lever demo", result="NOT RUN", reason="no postings returned")
            return
        apply_url = postings[0].get("applyUrl") or postings[0].get("hostedUrl")
        _run_canary("lever", "Lever demo", apply_url)
    except Exception as exc:  # noqa: BLE001
        _report("lever", "Lever demo", result="NOT RUN", reason=f"{type(exc).__name__}: {exc}")


def validate_ashby() -> None:
    try:
        resp = httpx.get("https://api.ashbyhq.com/posting-api/job-board/ashby", timeout=10.0)
        resp.raise_for_status()
        jobs = resp.json().get("jobs", [])
        if not jobs:
            _report("ashby", "Ashby", result="NOT RUN", reason="no jobs returned")
            return
        apply_url = jobs[0].get("applyUrl") or jobs[0].get("jobUrl")
        _run_canary("ashby", "Ashby", apply_url)
    except Exception as exc:  # noqa: BLE001
        _report("ashby", "Ashby", result="NOT RUN", reason=f"{type(exc).__name__}: {exc}")


def validate_workable() -> None:
    try:
        resp = httpx.get("https://apply.workable.com/api/v1/widget/accounts/flosum", timeout=10.0)
        resp.raise_for_status()
        jobs = resp.json().get("jobs", [])
        if not jobs:
            _report("workable", "Flosum", result="NOT RUN", reason="no jobs returned")
            return
        apply_url = jobs[0].get("application_url") or jobs[0].get("url")
        _run_canary("workable", "Flosum", apply_url)
    except Exception as exc:  # noqa: BLE001
        _report("workable", "Flosum", result="NOT RUN", reason=f"{type(exc).__name__}: {exc}")


def validate_smartrecruiters() -> None:
    company = "SmartRecruiters"
    try:
        resp = httpx.get(f"https://api.smartrecruiters.com/v1/companies/{company}/postings", timeout=10.0)
        resp.raise_for_status()
        content = resp.json().get("content", [])
        if not content:
            _report("smartrecruiters", company, result="NOT RUN", reason="no postings returned")
            return
        job_id = content[0].get("id", "")
        apply_url = f"https://jobs.smartrecruiters.com/{company}/{job_id}"
        _run_canary("smartrecruiters", company, apply_url)
    except Exception as exc:  # noqa: BLE001
        _report("smartrecruiters", company, result="NOT RUN", reason=f"{type(exc).__name__}: {exc}")


def validate_workday_repeated(attempts: int = 2, spacing_seconds: float = 3.0) -> None:
    """CLAUDE.md Phase 13 section 76: a small, bounded number of repeated
    observations -- never an excessive reload loop. Reuses Phase 12's
    Walmart tenant."""
    tenant, wd_host, site = "walmart", "wd504.myworkdayjobs.com", "WalmartExternal"
    company = f"Walmart ({tenant}.{wd_host}/{site})"
    try:
        cxs_url = f"https://{tenant}.{wd_host}/wday/cxs/{tenant}/{site}/jobs"
        resp = httpx.post(cxs_url, json={"limit": 1, "offset": 0}, timeout=10.0)
        resp.raise_for_status()
        postings = resp.json().get("jobPostings", [])
        if not postings:
            _report("workday", company, result="NOT RUN", reason="no postings returned")
            return
        path = postings[0].get("externalPath", "")
        apply_url = f"https://{tenant}.{wd_host}/{site}{path}"
        tenant_info = parse_workday_tenant(apply_url)
        for i in range(attempts):
            _run_canary("workday", f"{company} attempt {i + 1}/{attempts}", apply_url)
            if i < attempts - 1:
                time.sleep(spacing_seconds)
        stability = classify_stability(tenant_info.tenant or tenant, tenant_info.site or site)
        print(f"  workday tenant stability (from workday_tenant_attempts, if this script's sibling "
              f"phase12_live_validation.py has also run against this tenant): {stability.value}")
    except Exception as exc:  # noqa: BLE001
        _report("workday", company, result="NOT RUN", reason=f"{type(exc).__name__}: {exc}")


def main() -> None:
    init_db()
    print("Phase 13 bounded live canary validation -- read-only, no PII, no upload, no submit.")
    validate_greenhouse()
    validate_lever()
    validate_ashby()
    validate_workable()
    validate_smartrecruiters()
    validate_workday_repeated()

    print("\n\n=== SUMMARY ===")
    for row in RESULTS:
        print(f"  {row['provider']:<16} {row.get('company', ''):<45} "
              f"ok={row.get('ok', row.get('result'))}  captcha={row.get('captcha_detected')}  "
              f"login={row.get('login_detected')}  form={row.get('form_found')}  "
              f"upload_control={row.get('upload_control_found')}  final_submit={row.get('final_submit_found')}")


if __name__ == "__main__":
    main()
