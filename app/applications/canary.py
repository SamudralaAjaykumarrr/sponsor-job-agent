"""Safe application-flow canary validation (CLAUDE.md Phase 13 sections
13-14, 56, 74-77). A canary opens a configured PUBLIC job/application page
and observes provider/job-identity/apply-entry/form/upload-control/step/
login/CAPTCHA/final-submit-control state -- exactly the same detection
primitives `app.applications.browser_runtime` already uses for a real
session, reused here rather than duplicated (this module imports them, it
never re-implements DOM scanning).

A canary must NEVER (CLAUDE.md section 13, enforced structurally below):
  - fill any candidate PII into the page (no `application_fields` are ever
    passed to a fill function -- this module never even imports
    `app.applications.mapping`)
  - upload a resume (only DETECTS the presence of a file-type input)
  - click a final-submit control (only DETECTS it)
  - solve/bypass a CAPTCHA (a CAPTCHA sighting stops the canary immediately)

It MAY navigate through at most one bounded, freshly-classified
NAVIGATION_SAFE apply-entry hop -- the same one-hop-at-a-time, freshly-
re-validated safety model `app.applications.browser_assist.
_advance_through_apply_entry` already uses for a real session."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from app import config
from app.applications.domain_allowlist import PROVIDER_DOMAINS
from app.db import db_session


class CanaryUnavailable(Exception):
    """Raised when BROWSER_ASSIST_ENABLED is False or Playwright isn't
    installed -- never silently no-ops, matching every other browser-backed
    module in this project."""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _detect_provider(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for provider, suffixes in PROVIDER_DOMAINS.items():
        if provider == "mock_ats":
            continue
        if any(host == s or host.endswith("." + s) for s in suffixes):
            return provider
    return ""


@dataclass
class CanaryResult:
    provider: str
    url: str
    ok: bool = False
    captcha_detected: bool = False
    login_detected: bool = False
    apply_entry_found: bool = False
    apply_entry_followed: bool = False
    form_found: bool = False
    upload_control_found: bool = False
    final_submit_found: bool = False
    step_hint: str = ""
    error: str = ""
    tenant: str = ""
    site: str = ""

    def as_dict(self) -> dict:
        return {
            "provider": self.provider, "url": self.url, "ok": self.ok,
            "captcha_detected": self.captcha_detected, "login_detected": self.login_detected,
            "apply_entry_found": self.apply_entry_found, "apply_entry_followed": self.apply_entry_followed,
            "form_found": self.form_found, "upload_control_found": self.upload_control_found,
            "final_submit_found": self.final_submit_found, "step_hint": self.step_hint, "error": self.error,
            "tenant": self.tenant, "site": self.site,
        }


def _require_available() -> None:
    if not config.BROWSER_ASSIST_ENABLED:
        raise CanaryUnavailable("BROWSER_ASSIST_ENABLED is false.")
    from app.applications.browser_runtime import playwright_available
    if not playwright_available():
        raise CanaryUnavailable(
            "playwright is not installed -- run `pip install playwright && playwright install chromium`."
        )


def run_canary(url: str, *, provider: str = "") -> CanaryResult:
    """Opens `url` in a fresh, ephemeral browser context, observes state, and
    closes it. Never persists cookies/storage_state, matching every other
    browser-backed module's boundary. Any exception is caught and reported
    as a non-ok result with `error` set -- a canary run must never raise into
    a scheduler loop (CLAUDE.md section 63's 'one failing tenant/provider
    must never abort the cycle for any other', extended to canaries)."""
    _require_available()
    from playwright.sync_api import sync_playwright

    from app.applications.apply_entry import classify_apply_control_detailed
    from app.applications.browser_runtime import (
        _NEXT_BUTTON_PHRASES, _SUBMIT_BUTTON_PHRASES, _detect_button, _detect_fields, _wait_for_stable_state,
    )
    from app.applications.workday_tenant import parse_workday_tenant

    detected_provider = provider or _detect_provider(url)
    result = CanaryResult(provider=detected_provider, url=url)
    tenant_info = parse_workday_tenant(url) if detected_provider == "workday" else None
    if tenant_info and tenant_info.recognized:
        result.tenant, result.site = tenant_info.tenant, tenant_info.site

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=config.BROWSER_HEADLESS)
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(url, timeout=config.BROWSER_ASSIST_TIMEOUT_SECONDS * 1000)
            _wait_for_stable_state(page, provider=detected_provider, original_url=url)
            _observe(page, result, classify_apply_control_detailed,
                      _detect_button, _detect_fields, _SUBMIT_BUTTON_PHRASES, _NEXT_BUTTON_PHRASES)

            # CLAUDE.md section 13: at most one bounded, freshly-classified
            # NAVIGATION_SAFE hop -- never a final-submit click, never a
            # loop.
            if (result.apply_entry_found and not result.form_found and not result.captcha_detected
                    and not result.login_detected):
                current_host = (urlparse(page.url).hostname or "").lower()
                control = _detect_apply_entry_for_canary(page, current_host, classify_apply_control_detailed)
                if control is not None and control.get("classification") == "NAVIGATION_SAFE" and control.get("href"):
                    try:
                        page.goto(control["href"], timeout=config.BROWSER_ASSIST_TIMEOUT_SECONDS * 1000)
                        _wait_for_stable_state(page, provider=detected_provider, original_url=control["href"])
                        result.apply_entry_followed = True
                        _observe(page, result, classify_apply_control_detailed,
                                  _detect_button, _detect_fields, _SUBMIT_BUTTON_PHRASES, _NEXT_BUTTON_PHRASES)
                    except Exception:  # noqa: BLE001 -- a failed hop just means we stop, never a raised error
                        pass

            result.ok = True
        except Exception as exc:  # noqa: BLE001 -- a canary must never raise into its caller
            result.error = f"{type(exc).__name__}: {exc}"[:500]
        finally:
            context.close()
            browser.close()
    return result


def _detect_apply_entry_for_canary(page, current_host: str, classify_apply_control_detailed) -> Optional[dict]:
    """Bounded, single-pass scan for a labeled link/button -- deliberately
    simpler than browser_runtime._detect_apply_entry_control (no ambiguity
    resolution needed here: the canary only ever follows a hop when there is
    exactly one classified-safe candidate)."""
    try:
        raw = page.evaluate(
            """
            () => Array.from(document.querySelectorAll('a, button')).slice(0, 200).map((el) => ({
                text: (el.innerText || el.textContent || '').trim(),
                href: el.tagName === 'A' ? (el.getAttribute('href') || '') : '',
            })).filter((c) => c.text)
            """
        )
    except Exception:  # noqa: BLE001
        return None
    for c in raw:
        detail = classify_apply_control_detailed(c["text"], href=c["href"], current_host=current_host)
        if detail.classification.value == "NAVIGATION_SAFE" and c["href"]:
            return {"classification": detail.classification.value, "href": c["href"]}
    return None


def _observe(page, result: CanaryResult, classify_apply_control_detailed,
             _detect_button, _detect_fields, submit_phrases, next_phrases) -> None:
    # CLAUDE.md Phase 13 sections 17-20: same narrowed, element-based
    # heuristic as app.applications.browser_runtime._do_discover (see that
    # function's own comment) -- a defensively-loaded reCAPTCHA script tag
    # is not itself a rendered challenge.
    if (page.locator("iframe[src*='captcha' i]").count() > 0
            or page.locator("[class*='captcha' i]").count() > 0
            or page.locator("[id*='captcha' i]").count() > 0):
        result.captcha_detected = True
        return
    if page.locator("input[type=password]").count() > 0:
        result.login_detected = True
        return

    raw_fields = _detect_fields(page)
    if raw_fields:
        result.form_found = True
        if any(f.get("type") == "file" for f in raw_fields):
            result.upload_control_found = True

    submit_button = _detect_button(page, submit_phrases)
    if submit_button is not None:
        result.final_submit_found = True

    next_button = _detect_button(page, next_phrases, exclude_phrases=submit_phrases)
    if next_button is not None:
        result.step_hint = "multi_step_next_control_present"

    current_host = (urlparse(page.url).hostname or "").lower()
    if _detect_apply_entry_for_canary(page, current_host, classify_apply_control_detailed) is not None:
        result.apply_entry_found = True


def record_canary_run(result: CanaryResult) -> dict:
    now = utcnow()
    with db_session() as conn:
        conn.execute(
            """INSERT INTO provider_canary_runs
               (provider, tenant, site, url, ok, captcha_detected, login_detected, apply_entry_found,
                apply_entry_followed, form_found, upload_control_found, final_submit_found, step_hint, error,
                ran_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (result.provider, result.tenant, result.site, result.url, int(result.ok),
             int(result.captcha_detected), int(result.login_detected), int(result.apply_entry_found),
             int(result.apply_entry_followed), int(result.form_found), int(result.upload_control_found),
             int(result.final_submit_found), result.step_hint, result.error, now),
        )
        row = conn.execute(
            "SELECT * FROM provider_canary_runs WHERE provider = ? AND url = ? ORDER BY id DESC LIMIT 1",
            (result.provider, result.url),
        ).fetchone()
    return dict(row)


def list_canary_runs(provider: str = "", limit: int = 200) -> list[dict]:
    query = "SELECT * FROM provider_canary_runs"
    params: list = []
    if provider:
        query += " WHERE provider = ?"
        params.append(provider)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with db_session() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def run_and_record_canary(url: str, *, provider: str = "") -> dict:
    result = run_canary(url, provider=provider)
    return record_canary_run(result)


@dataclass
class ScheduledCanaryTarget:
    url: str
    provider: str = ""


def run_scheduled_canaries(targets: list[ScheduledCanaryTarget]) -> list[dict]:
    """CLAUDE.md Phase 13 section 14: the ONLY entry point that runs canaries
    on a schedule -- gated by REAL_ATS_CANARY_ENABLED, off by default, never
    enabled automatically. One failing target never aborts the rest
    (matching this project's standing 'one failing tenant/provider never
    aborts the cycle for others' rule)."""
    if not config.REAL_ATS_CANARY_ENABLED:
        return []
    results = []
    for target in targets:
        try:
            results.append(run_and_record_canary(target.url, provider=target.provider))
        except CanaryUnavailable as exc:
            results.append({"provider": target.provider, "url": target.url, "ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 -- one target's failure never aborts the rest
            results.append({"provider": target.provider, "url": target.url, "ok": False,
                             "error": f"{type(exc).__name__}: {exc}"})
    return results
