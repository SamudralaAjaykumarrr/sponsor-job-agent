"""CLAUDE.md Phase 12 sections 16-19, 53-54, 75: bounded, read-only, real-
public-ATS validation for the SPA/dynamic-flow hardening added this phase.
Extends scripts/phase11_live_validation.py's exact safety model -- opens AT
MOST ONE real posting per provider (Workday: bounded REPEATED loads of the
SAME posting, per CLAUDE.md section 18), never clicks a final submit
control, and never submits anything. Genuine findings are recorded into
app.applications.capability_evidence (REAL_BROWSER/REAL_BROWSER_REPEATED,
CLAUDE.md Phase 12 section 41) and app.applications.workday_tenant
(per-attempt, CLAUDE.md sections 19, 54) -- never inflated from memory or
assumption.

Every real URL below was found via a plain web search for publicly
documented, unauthenticated career-board URLs -- never guessed.

Usage:
    python scripts/phase12_live_validation.py

Requires network access and a launchable Chromium. If either is unavailable,
each provider's section reports NOT RUN with the reason rather than
fabricating a result."""

import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app import config

config.BROWSER_ASSIST_ENABLED = True
config.BROWSER_HEADLESS = True
config.REAL_ATS_VALIDATION_ENABLED = True
config.BROWSER_DOM_STABILIZATION_TIMEOUT_MS = 8000
config.BROWSER_DOM_STABILIZATION_POLL_MS = 250

from app.applications import browser_runtime  # noqa: E402
from app.applications.apply_entry import parse_step_progress  # noqa: E402
from app.applications.capability_evidence import EvidenceVerificationType, record_evidence  # noqa: E402
from app.applications.domain_allowlist import is_allowed_domain  # noqa: E402
from app.applications.trusted_redirects import RedirectTrust, classify_redirect_trust  # noqa: E402
from app.applications.workday_tenant import (  # noqa: E402
    classify_stability, parse_workday_tenant, record_attempt, record_observation,
)
from app.db import init_db  # noqa: E402

RESULTS: list[dict] = []


def _report(provider: str, company: str, **fields) -> None:
    row = {"provider": provider, "company": company, **fields}
    RESULTS.append(row)
    print(f"\n=== {provider} ({company}) ===")
    for k, v in fields.items():
        print(f"  {k}: {v}")


def _discover_and_follow_entry(provider: str, url: str) -> dict:
    """Opens `url`, uses the REAL bounded DOM-stabilization wait (never a
    blind sleep) instead of Phase 11's fixed `wait_for_timeout`, safely
    follows AT MOST ONE NAVIGATION_SAFE apply-entry control (the exact
    classification/trusted-redirect logic app.applications.apply_entry
    uses), scans same-origin/allowed-host iframes and open shadow roots via
    the SAME functions app.applications.browser_runtime uses in production,
    then runs the real DOM scan/detection code. Never clicks anything else."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        try:
            start = time.monotonic()
            page.goto(url, timeout=20000)
            stable = browser_runtime._wait_for_stable_state(page)

            landing_host = (urlparse(page.url).hostname or "").lower()
            apply_control = browser_runtime._detect_apply_entry_control(page, landing_host)
            entry_followed = False
            redirect_trust = (apply_control or {}).get("redirect_trust", "")
            if apply_control and not apply_control.get("ambiguous") \
                    and apply_control["classification"] == "NAVIGATION_SAFE":
                before_url = page.url
                try:
                    if apply_control.get("id"):
                        page.locator(f"#{apply_control['id']}").click(timeout=5000)
                    else:
                        page.get_by_text(apply_control["text"], exact=False).first.click(timeout=5000)
                    entry_followed = True
                except Exception:  # noqa: BLE001 -- a failed click just means "not followed"
                    pass
                if entry_followed:
                    browser_runtime._wait_for_stable_state(page)

            fields = browser_runtime._detect_fields(page)
            iframe_scan = browser_runtime._scan_iframes(page, provider, url)
            if iframe_scan["used"]:
                fields = fields + iframe_scan["fields"]
            shadow_used = browser_runtime._page_uses_shadow_dom(page)
            submit_button = (browser_runtime._detect_button(page, browser_runtime._SUBMIT_BUTTON_PHRASES)
                              or iframe_scan.get("submit_button"))
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
                "landing_render_wait": stable["reason"], "landing_render_ms": stable["elapsed_ms"],
                "apply_entry_control_found": apply_control is not None and not apply_control.get("ambiguous"),
                "apply_entry_ambiguous": bool(apply_control and apply_control.get("ambiguous")),
                "apply_entry_classification": (apply_control or {}).get("classification"),
                "apply_entry_redirect_trust": redirect_trust,
                "apply_entry_followed": entry_followed,
                "fields_detected": len(fields),
                "field_labels": [f["label"] for f in fields if f.get("label")][:15],
                "upload_field_detected": any(f.get("type") == "file" for f in fields),
                "iframe_used": iframe_scan["used"], "iframe_unexpected_host": iframe_scan["unexpected_host"],
                "shadow_dom_used": shadow_used,
                "submit_button_detected": submit_button is not None,
                "login_required": has_login, "captcha_observed": has_captcha,
                "step_progress_current": current_step, "step_progress_total": total_steps,
                "step_progress_confidence": confidence.value,
                "domain_allowlist_match": domain_ok,
                "total_elapsed_ms": int((time.monotonic() - start) * 1000),
            }
        finally:
            context.close()
            browser.close()


def _record_field_evidence(provider: str, result: dict, source_domain: str) -> None:
    if not result.get("opened"):
        return
    vtype = EvidenceVerificationType.REAL_BROWSER
    record_evidence(provider, "field_discovery", vtype, source_domain=source_domain,
                     notes=f"{result['fields_detected']} fields detected live")
    record_evidence(provider, "resume_upload",
                     vtype if result["upload_field_detected"] else EvidenceVerificationType.NOT_TESTED,
                     source_domain=source_domain, notes="upload field observed" if result["upload_field_detected"]
                     else "no upload field observed on this posting")
    # CLAUDE.md Phase 11 section 43 / Phase 12 section 41: "apply_first_click"
    # evidence is only ever REAL_BROWSER when a control was genuinely
    # classified NAVIGATION_SAFE AND successfully clicked/navigated -- a
    # control found but correctly left unclicked is NOT_TESTED, never
    # inflated to look like a proven capability.
    if result.get("apply_entry_followed"):
        record_evidence(provider, "apply_first_click", vtype, source_domain=source_domain,
                         notes=f"control classified {result['apply_entry_classification']} "
                               f"(redirect_trust={result.get('apply_entry_redirect_trust') or 'n/a'}), "
                               f"click-through to the real form succeeded")
    elif result.get("apply_entry_ambiguous"):
        record_evidence(provider, "apply_first_click", EvidenceVerificationType.NOT_TESTED, source_domain=source_domain,
                         notes="multiple NAVIGATION_SAFE apply controls with different destinations were found -- "
                               "correctly left unclicked rather than guessed")
    elif result.get("apply_entry_control_found"):
        record_evidence(provider, "apply_first_click", EvidenceVerificationType.NOT_TESTED, source_domain=source_domain,
                         notes=f"a control was found and classified {result['apply_entry_classification']} but "
                               f"was NOT followed (safety-correct for a non-NAVIGATION_SAFE classification, or a "
                               f"click attempt that did not complete within the bounded timeout)")
    record_evidence(provider, "login_handoff",
                     vtype if result["login_required"] else EvidenceVerificationType.NOT_TESTED,
                     source_domain=source_domain, notes="login wall observed" if result["login_required"]
                     else "no login wall on this posting")
    record_evidence(provider, "captcha_handoff",
                     vtype if result["captcha_observed"] else EvidenceVerificationType.NOT_TESTED,
                     source_domain=source_domain, notes="CAPTCHA observed" if result["captcha_observed"]
                     else "no CAPTCHA on this posting")
    if result.get("step_progress_confidence") == "EXACT":
        record_evidence(provider, "step_progress", vtype, source_domain=source_domain,
                         notes=f"step {result['step_progress_current']} of {result['step_progress_total']}")
    if result.get("iframe_used"):
        record_evidence(provider, "iframe_form_discovery", vtype, source_domain=source_domain,
                         notes="application form fields discovered inside a same-origin/allowed-host iframe")
    if result.get("shadow_dom_used"):
        record_evidence(provider, "shadow_dom_form_discovery", vtype, source_domain=source_domain,
                         notes="page uses an open shadow root")


def validate_greenhouse() -> None:
    """CLAUDE.md Phase 12 section 24: Greenhouse regression protection."""
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


def validate_gitlab_career_page_trusted_redirect() -> None:
    """CLAUDE.md Phase 12 sections 8-9, 63: opens GitLab's OWN corporate
    careers page (about.gitlab.com, NOT a greenhouse.io domain) and checks
    whether the real Apply-shaped controls it links out to actually resolve
    as TRUSTED_ATS_REDIRECT against the real destination host -- a genuine,
    real-world company-career-page-to-ATS-domain trust check, distinct from
    every other provider validation above (which all open the ATS's OWN
    domain directly). Never clicks anything -- classification only."""
    provider, company = "trusted_redirect", "GitLab corporate careers page (about.gitlab.com)"
    url = "https://about.gitlab.com/jobs/all-jobs/"
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_context().new_page()
            try:
                page.goto(url, timeout=20000)
                browser_runtime._wait_for_stable_state(page)
                # This specific page's job list renders progressively even
                # after the bounded stabilization wait's own settle
                # criteria are met (observed live this run) -- one extra
                # bounded wait for the actual anchor list to finish
                # populating, still far short of an unbounded wait.
                page.wait_for_timeout(2000)
                current_host = (urlparse(page.url).hostname or "").lower()
                candidates = page.evaluate(
                    "() => Array.from(document.querySelectorAll('a')).map(a => "
                    "({text: (a.innerText||'').trim(), href: a.getAttribute('href')||''}))"
                    ".filter(c => c.href && /greenhouse|lever|ashby|smartrecruiters|workday|workable/i.test(c.href))"
                )
                decisions = [
                    {"text": c["text"][:60], "href": c["href"],
                     "trust": classify_redirect_trust(current_host, c["href"]).trust.value}
                    for c in candidates[:10]
                ]
                trusted_count = sum(1 for d in decisions if d["trust"] == RedirectTrust.TRUSTED_ATS_REDIRECT.value)
                _report(provider, company, current_host=current_host, ats_links_found=len(decisions),
                        trusted_count=trusted_count, sample=decisions[:5])
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001
        _report(provider, company, result="NOT RUN", reason=f"{type(exc).__name__}: {exc}")


def validate_smartrecruiters_classic() -> None:
    """CLAUDE.md Phase 12 section 16: the classic postingUrl shape, same
    posting shape Phase 10/11 attempted."""
    provider, company = "smartrecruiters", "Visa (classic posting URL shape)"
    try:
        resp = httpx.get("https://api.smartrecruiters.com/v1/companies/Visa/postings", timeout=10.0)
        resp.raise_for_status()
        postings = resp.json().get("content", [])
        if not postings:
            _report(provider, company, result="NOT RUN", reason="no postings returned")
            return
        apply_url = postings[0].get("postingUrl") or postings[0].get("applyUrl")
        if not apply_url:
            _report(provider, company, result="NOT RUN", reason="no application URL in API response")
            return
        result = _discover_and_follow_entry(provider, apply_url)
        _record_field_evidence(provider, result, "jobs.smartrecruiters.com")
        _report(provider, company, application_url=apply_url, **result)
    except Exception as exc:  # noqa: BLE001
        _report(provider, company, result="NOT RUN", reason=f"{type(exc).__name__}: {exc}")


def validate_smartrecruiters_spa(oneclick_url: str = "") -> None:
    """CLAUDE.md Phase 12 section 16 (priority target): SmartRecruiters'
    NEWER 'oneclick-ui' client-rendered posting shape
    (jobs.smartrecruiters.com/oneclick-ui/company/<Company>/publication/
    <uuid>) -- found via a plain web search this phase, distinct from the
    classic shape Phase 10/11 could only reach as a landing page. This is
    the genuinely JS/SPA-rendered surface this phase's build brief calls
    out specifically."""
    provider, company = "smartrecruiters_spa", "SmartRecruiters oneclick-ui SPA posting"
    if not oneclick_url:
        _report(provider, company, result="NOT RUN", reason="no oneclick-ui URL supplied -- none discovered this run")
        return
    try:
        result = _discover_and_follow_entry("smartrecruiters", oneclick_url)
        _record_field_evidence("smartrecruiters", result, "jobs.smartrecruiters.com")
        _report(provider, company, application_url=oneclick_url, **result)
    except Exception as exc:  # noqa: BLE001
        _report(provider, company, result="NOT RUN", reason=f"{type(exc).__name__}: {exc}")


def validate_workday_repeated(attempts: int = 3, spacing_seconds: float = 3.0) -> None:
    """CLAUDE.md Phase 12 sections 18-21, 54, 77: repeats the SAME real
    Workday posting `attempts` times (bounded, conservative spacing) and
    classifies stability from the genuine results -- never cherry-picked to
    the more favorable run. Reuses the Phase 11 Walmart tenant."""
    provider = "workday"
    tenant, wd_host, site = "walmart", "wd504.myworkdayjobs.com", "WalmartExternal"
    company = f"Walmart ({tenant}.{wd_host}/{site}) -- {attempts} repeated observations"
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
        tenant_info = parse_workday_tenant(apply_url)
        results = []
        for i in range(attempts):
            result = _discover_and_follow_entry(provider, apply_url)
            results.append(result)
            apply_result = result.get("apply_entry_classification") or (
                "FORM_ALREADY_VISIBLE" if result.get("fields_detected") else "NONE"
            )
            record_attempt(
                tenant_info.tenant or tenant, tenant_info.site or site, tenant_info.host,
                requisition_id=tenant_info.requisition_id, url_initial=apply_url, url_final=result.get("final_url", ""),
                stage=("APPLICATION_FORM" if result.get("fields_detected") else "LANDING_PAGE"),
                apply_control_result=apply_result, render_time_ms=result.get("landing_render_ms"),
                fields_detected=result.get("fields_detected"), resume_upload_detected=result.get("upload_field_detected"),
                step_indicator=f"{result.get('step_progress_current')}/{result.get('step_progress_total')}"
                               if result.get("step_progress_current") else "",
                result=apply_result,
                notes=f"attempt {i + 1}/{attempts}",
            )
            if i < attempts - 1:
                time.sleep(spacing_seconds)
        _record_field_evidence(provider, results[-1], f"{tenant}.{wd_host}")
        record_observation(
            tenant_info.tenant or tenant, tenant_info.site or site, tenant_info.host,
            landing_navigation=any(r.get("opened") for r in results),
            login_required=results[-1].get("login_required"),
            resume_upload=results[-1].get("upload_field_detected"),
            notes=f"{attempts} repeated live observations this run",
        )
        stability = classify_stability(tenant_info.tenant or tenant, tenant_info.site or site)
        _report(provider, company, application_url=apply_url, attempts=attempts,
                per_attempt_results=[r.get("apply_entry_classification") or "form_visible" for r in results],
                stability=stability.value)
    except Exception as exc:  # noqa: BLE001
        _report(provider, company, result="NOT RUN", reason=f"{type(exc).__name__}: {exc}")


def validate_workable() -> None:
    """CLAUDE.md Phase 12 section 23: Workable regression protection."""
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
    """CLAUDE.md Phase 12 section 25: Lever redirect-behavior re-check."""
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
    """CLAUDE.md Phase 12 section 25: Ashby redirect-behavior re-check."""
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
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--smartrecruiters-spa-url", default="",
                         help="a real jobs.smartrecruiters.com/oneclick-ui/... URL to validate")
    parser.add_argument("--workday-attempts", type=int, default=3)
    args = parser.parse_args()

    init_db()
    print("Phase 12 bounded real-public-ATS validation -- read-only, one page per provider (Workday: repeated "
          "bounded loads of the same posting), at most one safe apply-entry click per open, never submits.")
    validate_greenhouse()
    validate_lever()
    validate_ashby()
    validate_workable()
    validate_smartrecruiters_classic()
    validate_smartrecruiters_spa(args.smartrecruiters_spa_url)
    validate_workday_repeated(attempts=args.workday_attempts)
    validate_gitlab_career_page_trusted_redirect()
    print("\n\n--- summary (JSON) ---")
    print(json.dumps(RESULTS, indent=2, default=str))


if __name__ == "__main__":
    main()
