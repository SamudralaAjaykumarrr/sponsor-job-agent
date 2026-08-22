"""Visible browser-assist layer (CLAUDE.md Phase 9 sections 21-23). OPTIONAL:
requires the `playwright` package AND its browser binaries
(`playwright install chromium`) installed separately -- never a default or
implicit dependency, and BROWSER_ASSIST_ENABLED defaults to False so nothing
here runs unless explicitly opted into.

Strict boundaries this module never crosses:
  - No stealth plugins, no browser-fingerprint spoofing, no CAPTCHA solving,
    no proxy rotation, no anti-bot bypass, no hidden/automated login, no MFA
    interception.
  - Never persists passwords, MFA codes, long-lived cookies, or auth tokens.
    Every browser context is a fresh, ephemeral, non-persistent
    `browser.new_context()` (never `launch_persistent_context()` with a
    reused profile directory, never `storage_state` saved to disk), closed
    at the end of every call regardless of outcome.
  - NEVER clicks a final "submit"/"apply" action, ever, under any
    condition -- this module only opens the page, detects fields (reusing
    the exact same deterministic app.applications.mapping engine every
    other provider adapter uses -- never a second, different matching
    heuristic), fills verified NON-sensitive values, prepares a resume/cover
    letter file upload, and stops the moment it hits a CAPTCHA, a
    login/MFA wall, or any field it cannot safely resolve.

Honest limitation (CLAUDE.md Phase 9 section 57): this MVP closes the
browser before returning a HandoffRecord -- it does not (yet) keep a visible
window open for the candidate to continue in-place. The HandoffRecord always
carries the real application URL so the candidate can open it themselves."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app import config
from app.applications.mapping import match_field
from app.applications.models import ApplicationField, FieldConfidence, SENSITIVE_CATEGORIES
from app.models import Job


class BrowserAssistUnavailable(Exception):
    """Raised (never silently swallowed) when BROWSER_ASSIST_ENABLED is
    False, or when Playwright / its browser binaries aren't installed."""


@dataclass
class HandoffRecord:
    """CLAUDE.md Phase 9 section 23: exactly what a NEEDS_USER_ACTION
    review queue entry should show."""
    job_id: int
    company: str
    application_url: str
    stage: str
    reason: str
    prepared_field_ids: list[str] = field(default_factory=list)
    unresolved_field_ids: list[str] = field(default_factory=list)
    resume_path: str = ""
    cover_letter_path: str = ""

    def as_dict(self) -> dict:
        return {
            "job_id": self.job_id, "company": self.company, "application_url": self.application_url,
            "stage": self.stage, "reason": self.reason, "prepared_field_ids": self.prepared_field_ids,
            "unresolved_field_ids": self.unresolved_field_ids, "resume_path": self.resume_path,
            "cover_letter_path": self.cover_letter_path,
        }


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def _require_playwright() -> None:
    if not config.BROWSER_ASSIST_ENABLED:
        raise BrowserAssistUnavailable("BROWSER_ASSIST_ENABLED is false.")
    if not playwright_available():
        raise BrowserAssistUnavailable(
            "playwright is not installed -- run `pip install playwright && playwright install chromium`."
        )


def _detect_fields(page) -> list[dict]:
    """Best-effort DOM scan for visible text/textarea/select/file inputs and
    their associated label text. Never touches password/hidden/checkbox/
    radio/submit/button inputs -- those are always left for the human."""
    return page.evaluate(
        """
        () => {
          const results = [];
          document.querySelectorAll('input, textarea, select').forEach((el, idx) => {
            const type = (el.getAttribute('type') || el.tagName).toLowerCase();
            if (['hidden', 'password', 'submit', 'button', 'checkbox', 'radio'].includes(type)) return;
            let label = '';
            if (el.labels && el.labels.length) label = el.labels[0].innerText;
            if (!label) label = el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.name || '';
            results.push({index: idx, label: (label || '').trim(), type: type, name: el.name || '', id: el.id || ''});
          });
          return results;
        }
        """
    )


def _selector_for(raw_field: dict) -> str:
    if raw_field["id"]:
        return f"#{raw_field['id']}"
    if raw_field["name"]:
        return f"[name='{raw_field['name']}']"
    return f":nth-match(input, textarea, select, {raw_field['index'] + 1})"


def prepare_application(
    job: Job, application_fields: list[ApplicationField], *, resume_path: str = "", cover_letter_path: str = "",
) -> HandoffRecord:
    """Opens the job's application URL in a browser (visible unless
    BROWSER_ASSIST_HEADLESS), detects fields, fills verified non-sensitive
    values, prepares a file upload, and stops at any user-required action.
    Raises BrowserAssistUnavailable if the feature/dependency isn't
    available -- never silently no-ops."""
    _require_playwright()
    from playwright.sync_api import sync_playwright

    url = job.canonical_url or job.url
    if not url:
        return HandoffRecord(
            job_id=job.id or 0, company=job.company, application_url="", stage="FORM_NOT_FOUND",
            reason="UNSUPPORTED_SUBMISSION", resume_path=resume_path, cover_letter_path=cover_letter_path,
        )

    prepared: list[str] = []
    unresolved: list[str] = []
    stage = "OPENED"
    reason = ""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=config.BROWSER_ASSIST_HEADLESS)
        # Ephemeral context ONLY -- never launch_persistent_context, never a
        # saved storage_state, so no cookie/session/token ever survives past
        # this single call.
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        try:
            page.goto(url, timeout=config.BROWSER_ASSIST_TIMEOUT_SECONDS * 1000)
            lowered = page.content().lower()
            if "captcha" in lowered or page.locator("iframe[src*='captcha']").count() > 0:
                stage, reason = "USER_ACTION_REQUIRED", "CAPTCHA_PRESENT"
            elif page.locator("input[type=password]").count() > 0:
                stage, reason = "USER_ACTION_REQUIRED", "LOGIN_REQUIRED"
            else:
                raw_fields = _detect_fields(page)
                for rf in raw_fields:
                    field_id, confidence = match_field(rf["label"], rf["name"])
                    app_field: Optional[ApplicationField] = (
                        next((f for f in application_fields if f.field_id == field_id), None) if field_id else None
                    )
                    label = rf["label"] or rf["name"] or f"field#{rf['index']}"
                    if app_field is None or not app_field.auto_fill_allowed or app_field.category in SENSITIVE_CATEGORIES:
                        unresolved.append(label)
                        continue
                    if confidence == FieldConfidence.LOW:
                        unresolved.append(label)
                        continue
                    selector = _selector_for(rf)
                    try:
                        if rf["type"] == "file":
                            if app_field.verified_value and Path(app_field.verified_value).exists():
                                page.locator(selector).set_input_files(app_field.verified_value)
                                prepared.append(app_field.field_id)
                            else:
                                unresolved.append(label)
                        else:
                            page.locator(selector).fill(str(app_field.verified_value))
                            prepared.append(app_field.field_id)
                    except Exception:  # noqa: BLE001 -- a single unfillable field must never abort the whole pass
                        unresolved.append(label)
                stage = "DRAFT_READY" if not unresolved else "USER_ACTION_REQUIRED"
                reason = "" if not unresolved else "UNRESOLVED_REQUIRED_FIELD"
        except Exception as exc:  # noqa: BLE001 -- navigation/timeout/DOM errors never crash the caller
            stage, reason = "USER_ACTION_REQUIRED", f"PLATFORM_POLICY_RESTRICTED: {type(exc).__name__}"
        finally:
            context.close()
            browser.close()

    return HandoffRecord(
        job_id=job.id or 0, company=job.company, application_url=url, stage=stage, reason=reason,
        prepared_field_ids=prepared, unresolved_field_ids=unresolved,
        resume_path=resume_path, cover_letter_path=cover_letter_path,
    )
