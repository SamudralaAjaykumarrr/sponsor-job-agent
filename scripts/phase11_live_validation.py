"""CLAUDE.md Phase 11 sections 9, 12, 14, 47-49: bounded, read-only, real-
public-ATS validation for the apply-first-click / Workday-tenant / Workable
hardening added this phase. Opens AT MOST ONE real posting per provider,
follows at most one apply-entry click (never a final submit), and runs the
exact app.applications.browser_runtime DOM-scanning/apply-entry-classification
code the product uses. NEVER submits anything. Genuine findings are recorded
into app.applications.capability_evidence (dated, LIVE_PUBLIC) and, for
Workday, app.applications.workday_tenant (per tenant/site, never a blanket
claim) -- never inflated from memory or assumption.

Every real tenant used below was found via a plain web search for publicly
documented, unauthenticated career-board URLs (the same kind of open
discovery app.registry.page_discovery already does) -- never guessed.

Usage:
    python scripts/phase11_live_validation.py

Requires network access and a launchable Chromium. If either is unavailable,
each provider's section reports NOT RUN with the reason rather than
fabricating a result.
"""

import json
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app import config

config.BROWSER_ASSIST_ENABLED = True
config.BROWSER_HEADLESS = True
config.REAL_ATS_VALIDATION_ENABLED = True

from app.applications import browser_runtime  # noqa: E402
from app.applications.apply_entry import parse_step_progress  # noqa: E402
from app.applications.capability_evidence import EvidenceVerificationType, record_evidence  # noqa: E402
from app.applications.domain_allowlist import is_allowed_domain  # noqa: E402
from app.applications.workday_tenant import parse_workday_tenant, record_observation  # noqa: E402
from app.db import init_db  # noqa: E402

RESULTS: list[dict] = []


def _report(provider: str, company: str, **fields) -> None:
    row = {"provider": provider, "company": company, **fields}
    RESULTS.append(row)
    print(f"\n=== {provider} ({company}) ===")
    for k, v in fields.items():
        print(f"  {k}: {v}")


def _discover_and_follow_entry(provider: str, url: str) -> dict:
    """Opens `url`, safely follows AT MOST ONE NAVIGATION_SAFE apply-entry
    control (the exact classification app.applications.apply_entry uses --
    never a second heuristic), then runs the real DOM scan/detection code
    the product uses. Never clicks anything else."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(url, timeout=20000)
            page.wait_for_timeout(1500)  # let client-rendered ATS widgets finish mounting

            landing_host = (urlparse(page.url).hostname or "").lower()
            apply_control = browser_runtime._detect_apply_entry_control(page, landing_host)
            entry_followed = False
            if apply_control and apply_control["classification"] == "NAVIGATION_SAFE":
                try:
                    if apply_control.get("id"):
                        page.locator(f"#{apply_control['id']}").click(timeout=5000)
                    else:
                        page.get_by_text(apply_control["text"], exact=False).first.click(timeout=5000)
                    page.wait_for_load_state("networkidle", timeout=8000)
                    entry_followed = True
                except Exception:  # noqa: BLE001 -- a failed click just means "not followed"
                    pass
                page.wait_for_timeout(1000)

            fields = browser_runtime._detect_fields(page)
            submit_button = browser_runtime._detect_button(page, browser_runtime._SUBMIT_BUTTON_PHRASES)
            has_login = page.locator("input[type=password]").count() > 0
            has_captcha = "captcha" in page.content().lower()
            try:
                body_text = page.inner_text("body")
            except Exception:  # noqa: BLE001
                body_text = ""
            current_step, total_steps, confidence = parse_step_progress(body_text)
            domain_ok = is_allowed_domain(provider, page.url)
            return {
                "opened": True, "landing_url": url, "final_url": page.url,
                "apply_entry_control_found": apply_control is not None,
                "apply_entry_classification": (apply_control or {}).get("classification"),
                "apply_entry_followed": entry_followed,
                "fields_detected": len(fields),
                "field_labels": [f["label"] for f in fields if f["label"]][:15],
                "upload_field_detected": any(f["type"] == "file" for f in fields),
                "submit_button_detected": submit_button is not None,
                "login_required": has_login, "captcha_observed": has_captcha,
                "step_progress_current": current_step, "step_progress_total": total_steps,
                "step_progress_confidence": confidence.value,
                "domain_allowlist_match": domain_ok,
            }
        finally:
            context.close()
            browser.close()


def _record_field_evidence(provider: str, result: dict, source_domain: str) -> None:
    if not result.get("opened"):
        return
    vtype = EvidenceVerificationType.LIVE_PUBLIC
    record_evidence(provider, "field_discovery", vtype, source_domain=source_domain,
                     notes=f"{result['fields_detected']} fields detected live")
    record_evidence(provider, "resume_upload", vtype if result["upload_field_detected"] else EvidenceVerificationType.NOT_TESTED,
                     source_domain=source_domain, notes="upload field observed" if result["upload_field_detected"]
                     else "no upload field observed on this posting")
    # "apply_first_click" capability evidence is LIVE_PUBLIC only when a
    # control was genuinely classified NAVIGATION_SAFE AND successfully
    # clicked/navigated -- a control that was found but correctly left
    # unclicked (EXTERNAL_REDIRECT/LOGIN_TRIGGER/UNKNOWN, the safety
    # mechanism working as intended) must never be reported the same as a
    # proven working click-through, even though both are genuine
    # observations worth recording.
    if result.get("apply_entry_followed"):
        record_evidence(provider, "apply_first_click", vtype, source_domain=source_domain,
                         notes=f"control classified {result['apply_entry_classification']}, click-through "
                               f"to the real form succeeded")
    elif result.get("apply_entry_control_found"):
        record_evidence(provider, "apply_first_click", EvidenceVerificationType.NOT_TESTED, source_domain=source_domain,
                         notes=f"a control was found and classified {result['apply_entry_classification']} but "
                               f"was NOT followed (safety-correct for a non-NAVIGATION_SAFE classification, or a "
                               f"click attempt that did not complete within the bounded timeout)")
    record_evidence(provider, "login_handoff", vtype if result["login_required"] else EvidenceVerificationType.NOT_TESTED,
                     source_domain=source_domain, notes="login wall observed" if result["login_required"]
                     else "no login wall on this posting")
    record_evidence(provider, "captcha_handoff", vtype if result["captcha_observed"] else EvidenceVerificationType.NOT_TESTED,
                     source_domain=source_domain, notes="CAPTCHA observed" if result["captcha_observed"]
                     else "no CAPTCHA on this posting")
    if result.get("step_progress_confidence") == "EXACT":
        record_evidence(provider, "step_progress", vtype, source_domain=source_domain,
                         notes=f"step {result['step_progress_current']} of {result['step_progress_total']}")


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
        apply_url = payload.get("absolute_url", "")
        if not apply_url:
            _report(provider, company, result="NOT RUN", reason="no application URL in API response")
            return
        result = _discover_and_follow_entry(provider, apply_url)
        _record_field_evidence(provider, result, "boards-api.greenhouse.io")
        _report(provider, company, application_url=apply_url, **result)
    except Exception as exc:  # noqa: BLE001 -- one provider's failure must never abort the others
        _report(provider, company, result="NOT RUN", reason=f"{type(exc).__name__}: {exc}")


def validate_smartrecruiters() -> None:
    """CLAUDE.md Phase 11 sections 8-9: this phase's headline hardening --
    Phase 10 could only reach the job-description LANDING page; this run
    also attempts to follow the real Apply control."""
    provider, company = "smartrecruiters", "SmartRecruiters' own board ('SmartRecruiters')"
    try:
        resp = httpx.get("https://api.smartrecruiters.com/v1/companies/SmartRecruiters/postings", timeout=10.0)
        resp.raise_for_status()
        postings = resp.json().get("content", [])
        if not postings:
            _report(provider, company, result="NOT RUN", reason="no postings returned")
            return
        apply_url = postings[0].get("postingUrl") or postings[0].get("applyUrl")
        if not apply_url:
            job_id = postings[0].get("id", "")
            apply_url = f"https://jobs.smartrecruiters.com/SmartRecruiters/{job_id}"
        result = _discover_and_follow_entry(provider, apply_url)
        _record_field_evidence(provider, result, "jobs.smartrecruiters.com")
        _report(provider, company, application_url=apply_url, **result)
    except Exception as exc:  # noqa: BLE001
        _report(provider, company, result="NOT RUN", reason=f"{type(exc).__name__}: {exc}")


def validate_workday() -> None:
    """CLAUDE.md Phase 11 sections 10-13, 45: a genuinely LIVE public
    Workday tenant found via web search (walmart.wd504.myworkdayjobs.com /
    WalmartExternal, a public careers board) -- the Phase 3/10 dogfood
    tenant (workday.wd5.myworkdayjobs.com) remains offline (re-checked this
    run: still 303 to community.workday.com/maintenance-page). Recorded
    PER TENANT/SITE, never as a blanket 'Workday supported' claim."""
    provider = "workday"
    tenant, wd_host, site = "walmart", "wd504.myworkdayjobs.com", "WalmartExternal"
    company = f"Walmart ({tenant}.{wd_host}/{site})"
    try:
        cxs_url = f"https://{tenant}.{wd_host}/wday/cxs/{tenant}/{site}/jobs"
        resp = httpx.post(cxs_url, json={"limit": 1, "offset": 0}, timeout=10.0)
        resp.raise_for_status()
        postings = resp.json().get("jobPostings", [])
        if not postings:
            _report(provider, company, result="NOT RUN", reason="no postings returned")
            return
        path = postings[0].get("externalPath", "")
        apply_url = f"https://{tenant}.{wd_host}/{site}{path}"
        result = _discover_and_follow_entry(provider, apply_url)
        _record_field_evidence(provider, result, f"{tenant}.{wd_host}")

        tenant_info = parse_workday_tenant(apply_url)
        record_observation(
            tenant_info.tenant or tenant, tenant_info.site or site, tenant_info.host,
            landing_navigation=result.get("opened", False),
            login_required=result.get("login_required"),
            resume_upload=result.get("upload_field_detected"),
            multi_step=(result.get("step_progress_total") or 0) > 1 or None,
            notes=f"{result.get('fields_detected', 0)} fields detected live via {apply_url}",
        )
        _report(provider, company, application_url=apply_url, **result)
    except Exception as exc:  # noqa: BLE001
        _report(provider, company, result="NOT RUN", reason=f"{type(exc).__name__}: {exc}")


def validate_workable() -> None:
    """CLAUDE.md Phase 11 section 14: a genuinely LIVE public Workable
    tenant found via web search ('flosum') -- Phase 3/10 never found a real
    tenant. The widget API's own `application_url` is already the
    candidate-facing apply form (no separate landing/apply-entry gate
    observed for this tenant -- reported honestly per-tenant, not
    generalized to all Workable accounts)."""
    provider, company = "workable", "Flosum (apply.workable.com/flosum)"
    try:
        resp = httpx.get("https://apply.workable.com/api/v1/widget/accounts/flosum", timeout=10.0)
        resp.raise_for_status()
        jobs = resp.json().get("jobs", [])
        if not jobs:
            _report(provider, company, result="NOT RUN", reason="no jobs returned")
            return
        apply_url = jobs[0].get("application_url") or jobs[0].get("url")
        if not apply_url:
            _report(provider, company, result="NOT RUN", reason="no application URL in API response")
            return
        result = _discover_and_follow_entry(provider, apply_url)
        _record_field_evidence(provider, result, "apply.workable.com")
        _report(provider, company, application_url=apply_url, **result)
    except Exception as exc:  # noqa: BLE001
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
        result = _discover_and_follow_entry(provider, apply_url)
        _record_field_evidence(provider, result, "jobs.lever.co")
        _report(provider, company, application_url=apply_url, **result)
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
        result = _discover_and_follow_entry(provider, apply_url)
        _record_field_evidence(provider, result, "jobs.ashbyhq.com")
        _report(provider, company, application_url=apply_url, **result)
    except Exception as exc:  # noqa: BLE001
        _report(provider, company, result="NOT RUN", reason=f"{type(exc).__name__}: {exc}")


def main() -> None:
    init_db()
    print("Phase 11 bounded real-public-ATS validation -- read-only, one page per provider, "
          "at most one safe apply-entry click, never submits.")
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
