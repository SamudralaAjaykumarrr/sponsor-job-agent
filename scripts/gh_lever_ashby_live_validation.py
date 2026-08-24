"""Bounded, read-only, real-public-ATS validation for the Greenhouse/Lever/
Ashby hardening pass on branch feat/providers-greenhouse-lever-ashby.

Reuses the EXACT same safety model and the EXACT same already-vetted real
public postings scripts/phase12_live_validation.py and
scripts/phase13_live_validation.py established (GitLab's public Greenhouse
board, Lever's own demo account, Ashby's own careers board) -- never a
guessed or new URL. At most ONE posting opened per provider, at most ONE
safe NAVIGATION_SAFE apply-entry click followed, never fills any candidate
field, never uploads a resume, never clicks a final-submit control, never
submits anything.

Adds one thing neither prior script did: a live JOB-IDENTITY check --
compares the "stored" signals this hardening pass's fixed discovery-layer
normalizer (app.providers.{greenhouse,lever,ashby}) would produce for the
posting against "observed" signals read straight off the real rendered
page, via the same app.applications.job_identity.verify_job_identity_full()
the executor itself calls before a resume upload / final submit. This is
also the first live exercise of this pass's own job_identity.py UUID-path
requisition-token fix against real Lever/Ashby URLs (previously covered by
deterministic fixture-string tests only).

Usage:
    python scripts/gh_lever_ashby_live_validation.py

Requires network access and a launchable Chromium. If either is
unavailable, each provider's section reports NOT RUN with the real reason
rather than fabricating a result."""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app import config

config.BROWSER_ASSIST_ENABLED = True
config.BROWSER_HEADLESS = True
config.REAL_ATS_VALIDATION_ENABLED = True
config.REAL_ATS_CANARY_ENABLED = True
config.BROWSER_DOM_STABILIZATION_TIMEOUT_MS = 8000
config.BROWSER_DOM_STABILIZATION_POLL_MS = 250

from app.applications import browser_runtime, canary  # noqa: E402
from app.applications.capability_evidence import EvidenceVerificationType, record_evidence  # noqa: E402
from app.applications.job_identity import (  # noqa: E402
    JobIdentitySignals, extract_requisition_token, verify_job_identity_full,
)
from app.db import init_db  # noqa: E402
from app.providers.ashby import AshbyProvider  # noqa: E402
from app.providers.greenhouse import GreenhouseProvider  # noqa: E402
from app.providers.lever import LeverProvider  # noqa: E402

RESULTS: list[dict] = []


def _report(provider: str, company: str, **fields) -> None:
    row = {"provider": provider, "company": company, **fields}
    RESULTS.append(row)
    print(f"\n=== {provider} ({company}) ===")
    for k, v in fields.items():
        print(f"  {k}: {v}")


def _discover(provider: str, url: str) -> dict:
    """Same bounded, read-only DOM pass scripts/phase12_live_validation.py
    uses: real DOM-stabilization wait (never a blind sleep), at most one
    safe apply-entry hop, real field/upload/CAPTCHA/login detection, never
    fills or submits."""
    from playwright.sync_api import sync_playwright
    from urllib.parse import urlparse

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
            if apply_control and not apply_control.get("ambiguous") \
                    and apply_control["classification"] == "NAVIGATION_SAFE":
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
            submit_button = browser_runtime._detect_button(page, browser_runtime._SUBMIT_BUTTON_PHRASES)
            has_login = page.locator("input[type=password]").count() > 0
            # Diagnostic-only, whole-page-text scan -- deliberately NOT what
            # production uses (browser_runtime/canary use a DOM-element-based
            # check: iframe[src*='captcha']/[class*='captcha']/[id*='captcha'],
            # exactly per CLAUDE.md Phase 13's rule against a text-substring
            # scan, which is known to false-positive on a merely-referenced,
            # never-rendered CAPTCHA script tag). Reported separately from
            # canary_captcha_detected below so the two can be compared honestly.
            has_captcha = "captcha" in page.content().lower()
            page_title = ""
            try:
                page_title = page.title()
            except Exception:  # noqa: BLE001
                pass
            # The REAL signal app.applications.browser_runtime itself uses for
            # job-identity verification before a resume upload / final submit
            # -- schema.org JSON-LD JobPosting data, never the raw <title> tag
            # (which commonly carries site-chrome wrapper text like "Job
            # Application for X at Company"). Used for the identity check
            # below instead of page_title, matching production exactly.
            observed_meta = browser_runtime._extract_observed_job_meta(page)
            return {
                "opened": True, "landing_url": url, "final_url": page.url, "page_title": page_title,
                "observed_jsonld_meta": observed_meta,
                "landing_render_wait": stable["reason"], "landing_render_ms": stable["elapsed_ms"],
                "apply_entry_control_found": apply_control is not None and not apply_control.get("ambiguous"),
                "apply_entry_classification": (apply_control or {}).get("classification"),
                "apply_entry_followed": entry_followed,
                "fields_detected": len(fields),
                "field_labels": [f["label"] for f in fields if f.get("label")][:12],
                "upload_field_detected": any(f.get("type") == "file" for f in fields),
                "submit_button_detected": submit_button is not None,
                "login_required": has_login, "captcha_observed": has_captcha,
                "total_elapsed_ms": int((time.monotonic() - start) * 1000),
            }
        finally:
            context.close()
            browser.close()


def _identity_check(provider: str, stored_title: str, stored_company: str, stored_url: str,
                     observed: dict) -> dict:
    """Live exercise of verify_job_identity_full() using the EXACT same
    observed-signal source app.applications.browser_runtime itself uses
    before a resume upload / final submit (schema.org JSON-LD JobPosting
    data via _extract_observed_job_meta, not the raw <title> tag -- an
    earlier version of this script used page.title() directly, which
    produced a false MISMATCH from site-chrome wrapper text like "Job
    Application for X at Company"; this is the honest, production-faithful
    check). 'stored' is what this pass's fixed discovery normalizer
    produced for the posting."""
    meta = observed.get("observed_jsonld_meta") or {}
    stored = JobIdentitySignals(title=stored_title, company=stored_company, provider=provider, url=stored_url)
    observed_signals = JobIdentitySignals(
        title=meta.get("title", ""), company=meta.get("company", ""),
        requisition_id=meta.get("identifier", ""), provider=provider, url=observed.get("final_url", ""),
    )
    verification = verify_job_identity_full(stored, observed_signals)
    return {
        "identity_verdict": verification.verdict.value,
        "identity_signals_compared": list(verification.signals_compared),
        "identity_signals_matched": list(verification.signals_matched),
        "identity_reason": verification.reason,
        "jsonld_present": bool(meta),
        "stored_requisition_token": extract_requisition_token(stored_url),
        "observed_requisition_token": extract_requisition_token(observed.get("final_url", "")),
    }


def _record_evidence(provider: str, result: dict, source_domain: str) -> None:
    vtype = EvidenceVerificationType.REAL_BROWSER
    record_evidence(provider, "field_discovery", vtype, source_domain=source_domain,
                     notes=f"{result['fields_detected']} fields detected live "
                           f"(gh-lever-ashby-hardening pass re-check)")
    record_evidence(provider, "resume_upload",
                     vtype if result["upload_field_detected"] else EvidenceVerificationType.NOT_TESTED,
                     source_domain=source_domain, notes="upload field observed" if result["upload_field_detected"]
                     else "no upload field observed on this posting")
    record_evidence(provider, "login_handoff",
                     vtype if result["login_required"] else EvidenceVerificationType.NOT_TESTED,
                     source_domain=source_domain, notes="login wall observed" if result["login_required"]
                     else "no login wall on this posting")
    record_evidence(provider, "captcha_handoff",
                     vtype if result["captcha_observed"] else EvidenceVerificationType.NOT_TESTED,
                     source_domain=source_domain, notes="CAPTCHA observed" if result["captcha_observed"]
                     else "no CAPTCHA on this posting")


def validate_greenhouse() -> None:
    provider, company = "greenhouse", "GitLab (public board token 'gitlab')"
    try:
        gh = GreenhouseProvider(["gitlab"])
        jobs = gh.fetch_jobs(max_jobs=1)
        if not jobs:
            _report(provider, company, result="NOT RUN", reason="no jobs returned by the public API")
            return
        job = jobs[0]
        result = _discover(provider, job.url)
        identity = _identity_check(provider, job.title, job.company, job.url, result)
        _record_evidence(provider, result, "boards-api.greenhouse.io / job-boards.greenhouse.io")
        canary_result = canary.run_and_record_canary(job.url, provider=provider)
        _report(provider, company, application_url=job.url,
                stored_first_published=job.published_at, stored_requisition_id=job.provider_metadata.get("requisition_id"),
                **result, **identity,
                canary_captcha_detected=canary_result.get("captcha_detected"),
                canary_login_detected=canary_result.get("login_detected"),
                canary_form_found=canary_result.get("form_found"),
                canary_final_submit_found=canary_result.get("final_submit_found"))
    except Exception as exc:  # noqa: BLE001 -- one provider's failure must never abort the others
        _report(provider, company, result="NOT RUN", reason=f"{type(exc).__name__}: {exc}")


def validate_lever() -> None:
    provider, company = "lever", "Lever's own demo account ('leverdemo')"
    try:
        lv = LeverProvider(["leverdemo"])
        jobs = lv.fetch_jobs(max_jobs=1)
        if not jobs:
            _report(provider, company, result="NOT RUN", reason="no postings returned")
            return
        job = jobs[0]
        result = _discover(provider, job.url)
        identity = _identity_check(provider, job.title, job.company, job.url, result)
        _record_evidence(provider, result, "jobs.lever.co")
        canary_result = canary.run_and_record_canary(job.url, provider=provider)
        _report(provider, company, application_url=job.url,
                stored_salary_period=job.salary_period, stored_description_len=len(job.description),
                **result, **identity,
                canary_captcha_detected=canary_result.get("captcha_detected"),
                canary_login_detected=canary_result.get("login_detected"),
                canary_form_found=canary_result.get("form_found"),
                canary_final_submit_found=canary_result.get("final_submit_found"))
    except Exception as exc:  # noqa: BLE001
        _report(provider, company, result="NOT RUN", reason=f"{type(exc).__name__}: {exc}")


def validate_ashby() -> None:
    provider, company = "ashby", "Ashby's own careers board ('ashby')"
    try:
        ab = AshbyProvider(["ashby"])
        jobs = ab.fetch_jobs(max_jobs=1)
        if not jobs:
            _report(provider, company, result="NOT RUN", reason="no jobs returned")
            return
        job = None
        for candidate in jobs:
            if candidate.url:
                job = candidate
                break
        if job is None:
            _report(provider, company, result="NOT RUN", reason="no job with a usable apply URL")
            return
        result = _discover(provider, job.url)
        identity = _identity_check(provider, job.title, job.company, job.url, result)
        _record_evidence(provider, result, "jobs.ashbyhq.com")
        canary_result = canary.run_and_record_canary(job.url, provider=provider)
        _report(provider, company, application_url=job.url,
                stored_salary_min=job.salary_min, stored_salary_max=job.salary_max,
                stored_salary_currency=job.salary_currency, stored_salary_period=job.salary_period,
                **result, **identity,
                canary_captcha_detected=canary_result.get("captcha_detected"),
                canary_login_detected=canary_result.get("login_detected"),
                canary_form_found=canary_result.get("form_found"),
                canary_final_submit_found=canary_result.get("final_submit_found"))
    except Exception as exc:  # noqa: BLE001
        _report(provider, company, result="NOT RUN", reason=f"{type(exc).__name__}: {exc}")


def main() -> None:
    init_db()
    print("gh-lever-ashby-hardening bounded live validation -- read-only, no PII, no upload, no submit, "
          "at most one posting per provider, reusing already-vetted public URLs.")
    validate_greenhouse()
    validate_lever()
    validate_ashby()

    print("\n\n--- summary (JSON) ---")
    print(json.dumps(RESULTS, indent=2, default=str))


if __name__ == "__main__":
    main()
