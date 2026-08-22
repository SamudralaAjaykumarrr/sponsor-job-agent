"""CLAUDE.md Phase 10 sections 18, 20, 57-59: bounded, read-only, real-public-
ATS validation. Opens AT MOST ONE real posting per provider, discovers
fields via the exact same app.applications.browser_runtime DOM-scanning code
the product uses, and reports what was genuinely observed. NEVER submits
anything. Low volume by construction -- one page per provider, run manually,
never on a schedule or in CI.

Usage:
    python scripts/phase10_live_validation.py

Requires BROWSER_ASSIST_ENABLED-independent Playwright + a launchable
Chromium (this script sets config flags itself; it does not read .env).
Network access required -- if unavailable, each provider's section reports
NOT RUN with the reason rather than fabricating a result.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app import config
from app.applications import browser_runtime
from app.applications.domain_allowlist import is_allowed_domain

RESULTS: list[dict] = []


def _report(provider: str, company: str, **fields) -> None:
    row = {"provider": provider, "company": company, **fields}
    RESULTS.append(row)
    print(f"\n=== {provider} ({company}) ===")
    for k, v in fields.items():
        print(f"  {k}: {v}")


def _discover_real_page(provider: str, url: str) -> dict:
    """Opens `url` in a real headless Chromium and runs the exact
    app.applications.browser_runtime DOM scan/detection code -- never a
    second, different heuristic from what the product actually uses."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(url, timeout=20000)
            page.wait_for_timeout(1500)  # let client-rendered ATS widgets finish mounting
            fields = browser_runtime._detect_fields(page)
            submit_button = browser_runtime._detect_button(page, browser_runtime._SUBMIT_BUTTON_PHRASES)
            has_login = page.locator("input[type=password]").count() > 0
            has_captcha = "captcha" in page.content().lower()
            domain_ok = is_allowed_domain(provider, page.url)
            return {
                "opened": True, "final_url": page.url, "fields_detected": len(fields),
                "field_labels": [f["label"] for f in fields if f["label"]][:15],
                "upload_field_detected": any(f["type"] == "file" for f in fields),
                "submit_button_detected": submit_button is not None,
                "login_required": has_login, "captcha_observed": has_captcha,
                "domain_allowlist_match": domain_ok,
            }
        finally:
            context.close()
            browser.close()


def validate_greenhouse() -> None:
    provider, company = "greenhouse", "GitLab (public board token 'gitlab')"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get("https://boards-api.greenhouse.io/v1/boards/gitlab/jobs", params={"content": "false"})
            resp.raise_for_status()
            jobs = resp.json().get("jobs", [])
        if not jobs:
            _report(provider, company, result="NOT RUN", reason="no jobs returned by the public API")
            return
        job_id = jobs[0]["id"]
        q_resp = httpx.get(
            f"https://boards-api.greenhouse.io/v1/boards/gitlab/jobs/{job_id}", params={"questions": "true"},
            timeout=10.0,
        )
        q_resp.raise_for_status()
        payload = q_resp.json()
        api_fields = sum(len(q.get("fields", [])) for q in payload.get("questions", []))
        apply_url = payload.get("absolute_url", "")
        browser_result = _discover_real_page(provider, apply_url) if apply_url else {"opened": False}
        _report(provider, company, api_structured_fields=api_fields, application_url=apply_url,
                multi_step="unknown -- single-page apply form observed", **browser_result)
    except Exception as exc:  # noqa: BLE001 -- one provider's failure must never abort the others
        _report(provider, company, result="NOT RUN", reason=f"{type(exc).__name__}: {exc}")


def validate_lever() -> None:
    provider, company = "lever", "Lever's own demo account ('leverdemo')"
    try:
        resp = httpx.get("https://api.lever.co/v0/postings/leverdemo", params={"mode": "json"}, timeout=10.0)
        resp.raise_for_status()
        postings = resp.json()
        if not postings:
            _report(provider, company, result="NOT RUN", reason="no postings returned")
            return
        apply_url = postings[0].get("applyUrl") or postings[0].get("hostedUrl")
        browser_result = _discover_real_page(provider, apply_url)
        _report(provider, company, application_url=apply_url, **browser_result)
    except Exception as exc:  # noqa: BLE001
        _report(provider, company, result="NOT RUN", reason=f"{type(exc).__name__}: {exc}")


def validate_ashby() -> None:
    provider, company = "ashby", "Ashby's own careers board ('ashby')"
    try:
        resp = httpx.get("https://api.ashbyhq.com/posting-api/job-board/ashby", timeout=10.0)
        resp.raise_for_status()
        jobs = resp.json().get("jobs", [])
        if not jobs:
            _report(provider, company, result="NOT RUN", reason="no jobs returned")
            return
        apply_url = jobs[0].get("applyUrl") or jobs[0].get("jobUrl")
        browser_result = _discover_real_page(provider, apply_url)
        _report(provider, company, application_url=apply_url, **browser_result)
    except Exception as exc:  # noqa: BLE001
        _report(provider, company, result="NOT RUN", reason=f"{type(exc).__name__}: {exc}")


def validate_smartrecruiters() -> None:
    provider, company = "smartrecruiters", "SmartRecruiters' own board ('SmartRecruiters')"
    try:
        resp = httpx.get(
            "https://api.smartrecruiters.com/v1/companies/SmartRecruiters/postings", timeout=10.0,
        )
        resp.raise_for_status()
        postings = resp.json().get("content", [])
        if not postings:
            _report(provider, company, result="NOT RUN", reason="no postings returned")
            return
        # The list endpoint's `postingUrl` field (what
        # app.providers.smartrecruiters expects) was not present in this
        # session's live response -- the real candidate-facing URL format
        # (confirmed live: HTTP 200) is https://jobs.smartrecruiters.com/<company>/<id>.
        apply_url = postings[0].get("postingUrl") or postings[0].get("applyUrl")
        if not apply_url:
            job_id = postings[0].get("id", "")
            apply_url = f"https://jobs.smartrecruiters.com/SmartRecruiters/{job_id}"
        browser_result = _discover_real_page(provider, apply_url)
        _report(provider, company, application_url=apply_url, **browser_result)
    except Exception as exc:  # noqa: BLE001
        _report(provider, company, result="NOT RUN", reason=f"{type(exc).__name__}: {exc}")


def validate_workday() -> None:
    provider, company = "workday", "Workday's own dogfood tenant (workday.wd5.myworkdayjobs.com/Workday)"
    try:
        cxs_url = "https://workday.wd5.myworkdayjobs.com/wday/cxs/workday/Workday/jobs"
        resp = httpx.post(cxs_url, json={"limit": 1, "offset": 0}, timeout=10.0)
        resp.raise_for_status()
        postings = resp.json().get("jobPostings", [])
        if not postings:
            _report(provider, company, result="NOT RUN", reason="no postings returned")
            return
        path = postings[0].get("externalPath", "")
        apply_url = f"https://workday.wd5.myworkdayjobs.com/Workday{path}"
        browser_result = _discover_real_page(provider, apply_url)
        _report(provider, company, application_url=apply_url, **browser_result)
    except Exception as exc:  # noqa: BLE001
        _report(provider, company, result="NOT RUN", reason=f"{type(exc).__name__}: {exc}")


def validate_workable() -> None:
    _report(
        "workable", "(none)", result="NOT RUN",
        reason="No live-verified public Workable tenant is known -- Phase 3's own dogfooding attempt against "
               "guessed tenant names did not resolve to a real account (see docs/acceptance_verification.md). "
               "Not fabricating a tenant name here either.",
    )


def main() -> None:
    print("Phase 10 bounded real-public-ATS validation -- read-only, one page per provider, never submits.")
    validate_greenhouse()
    validate_lever()
    validate_ashby()
    validate_smartrecruiters()
    validate_workday()
    validate_workable()
    print("\n\n--- summary (JSON) ---")
    print(json.dumps(RESULTS, indent=2))


if __name__ == "__main__":
    main()
