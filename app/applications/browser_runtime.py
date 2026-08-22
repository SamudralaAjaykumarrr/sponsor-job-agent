"""Playwright-backed browser runtime (CLAUDE.md Phase 10 sections 3, 9-13,
30-41). This module is the ONLY place that ever touches Playwright directly;
app.applications.browser_assist orchestrates sessions/state and never imports
`playwright` itself.

Strict boundaries this module never crosses (CLAUDE.md Phase 9 sections
21-23, reaffirmed for Phase 10 sections 3, 68):
  - No stealth plugins, no fingerprint spoofing, no CAPTCHA solving, no proxy
    rotation, no anti-bot bypass, no hidden/automated login, no MFA
    interception.
  - Never persists a password, MFA code, cookie, or raw auth token. Every
    browser context is a fresh, ephemeral `browser.new_context()` -- never
    `launch_persistent_context()` with a reused profile directory, never a
    saved `storage_state`.
  - NEVER clicks a final submit/apply action. `detect_submit_button()` only
    locates and reports it; nothing in this module invokes `.click()` on it.
  - Only ever navigates within the job's own application host or that
    provider's known domain allowlist (app.applications.domain_allowlist) --
    an unexpected destination pauses the session for review instead of
    continuing to interact with an unverified page.

"Persistent window" model: each live session owns a dedicated single-thread
executor (Playwright's sync API must be driven from one consistent thread)
that keeps the browser/context/page open across multiple separate calls into
this module -- e.g. one dashboard request opens the browser and discovers the
form, a LATER dashboard request (after the candidate logs in by hand in that
same visible window) calls `rediscover()` against the SAME live page. This
only works while the owning worker/dashboard PROCESS stays alive; if that
process restarts, the live registry is empty and the caller
(app.applications.browser_assist.resume_session) honestly falls back to
either a fresh `open_session()` (safe pre-submission) or
SUBMISSION_STATUS_UNKNOWN (if a submission may have been in flight) -- this
module makes no claim of cross-process browser reattachment."""

import hashlib
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app import config
from app.applications.domain_allowlist import is_allowed_host_for_session
from app.applications.mapping import match_field
from app.applications.models import ApplicationField, FieldConfidence, SENSITIVE_CATEGORIES
from app.applications.schema import DECLINE_TO_SELF_IDENTIFY_PHRASES, find_field


class BrowserRuntimeUnavailable(Exception):
    """Raised when BROWSER_ASSIST_ENABLED is False, or Playwright / its
    browser binaries aren't installed -- never silently swallowed."""


class BrowserRuntimeBusy(Exception):
    """Raised when BROWSER_ASSIST_CONCURRENCY's bound is already reached
    (CLAUDE.md Phase 10 section 45) -- browser sessions are expensive and
    interactive, so this is a hard cap, not a queue-and-wait."""


_NEXT_BUTTON_PHRASES = ("next", "continue", "save and continue", "next step")
_SUBMIT_BUTTON_PHRASES = ("submit application", "submit your application", "apply now", "submit", "send application")
_MFA_PHRASES = ("verification code", "authentication code", "two-factor", "2fa", "one-time code", "otp")
_SUCCESS_PHRASES = (
    "thank you for applying", "application received", "application submitted", "successfully applied",
    "we've received your application", "we have received your application",
    "your application has been submitted", "thank you for your application", "thank you -- your application",
)
_CONFIRMATION_ID_RE = re.compile(r"(?:confirmation|reference|application)\s*(?:number|id|#)?\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-]{3,})", re.I)


@dataclass
class DiscoveryOutcome:
    pause_reason: Optional[str] = None
    current_url: str = ""
    fields: list[dict] = field(default_factory=list)
    fingerprint: str = ""
    submit_button: Optional[dict] = None
    next_button: Optional[dict] = None
    total_steps_hint: int = 1


@dataclass
class FillOutcome:
    filled: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    uploads: list[str] = field(default_factory=list)


@dataclass
class ConfirmationOutcome:
    confirmed: bool = False
    current_url: str = ""
    confirmation_id: str = ""
    confirmation_text_fingerprint: str = ""


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def _require_available() -> None:
    if not config.BROWSER_ASSIST_ENABLED:
        raise BrowserRuntimeUnavailable("BROWSER_ASSIST_ENABLED is false.")
    if not playwright_available():
        raise BrowserRuntimeUnavailable(
            "playwright is not installed -- run `pip install playwright && playwright install chromium`."
        )


class _LiveSession:
    def __init__(self, session_id: str, provider: str, application_url: str):
        self.session_id = session_id
        self.provider = provider
        self.application_url = application_url
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"bsess-{session_id[:16]}")
        self._pw_cm = None
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.current_step = 1

    def run(self, fn, *args, timeout: float = 60.0, **kwargs):
        future = self.executor.submit(fn, *args, **kwargs)
        return future.result(timeout=timeout)

    # --- operations executed INSIDE the dedicated thread --------------------

    def _do_open(self, url: str) -> None:
        from playwright.sync_api import sync_playwright

        self._pw_cm = sync_playwright()
        self.playwright = self._pw_cm.__enter__()
        self.browser = self.playwright.chromium.launch(headless=config.BROWSER_HEADLESS)
        # Ephemeral context ONLY -- see module docstring.
        self.context = self.browser.new_context(accept_downloads=True)
        self.page = self.context.new_page()
        self.page.goto(url, timeout=config.BROWSER_ASSIST_TIMEOUT_SECONDS * 1000)

    def _do_discover(self) -> DiscoveryOutcome:
        page = self.page
        current_url = page.url
        if not is_allowed_host_for_session(self.provider, self.application_url, current_url):
            return DiscoveryOutcome(pause_reason="PLATFORM_POLICY_RESTRICTED", current_url=current_url)

        content_lower = page.content().lower()
        has_captcha = (
            "captcha" in content_lower
            or page.locator("iframe[src*='captcha' i]").count() > 0
            or page.locator("[class*='captcha' i]").count() > 0
            or page.locator("[id*='captcha' i]").count() > 0
        )
        if has_captcha:
            return DiscoveryOutcome(pause_reason="CAPTCHA_PRESENT", current_url=current_url)

        if page.locator("input[type=password]").count() > 0:
            if any(p in content_lower for p in _MFA_PHRASES):
                return DiscoveryOutcome(pause_reason="MFA_REQUIRED", current_url=current_url)
            return DiscoveryOutcome(pause_reason="LOGIN_REQUIRED", current_url=current_url)

        raw_fields = _detect_fields(page)
        submit_button = _detect_button(page, _SUBMIT_BUTTON_PHRASES)
        next_button = _detect_button(page, _NEXT_BUTTON_PHRASES, exclude_phrases=_SUBMIT_BUTTON_PHRASES)
        fingerprint = _fingerprint_fields(raw_fields)
        return DiscoveryOutcome(
            pause_reason=None, current_url=current_url, fields=raw_fields, fingerprint=fingerprint,
            submit_button=submit_button, next_button=next_button,
            total_steps_hint=2 if next_button else 1,
        )

    def _do_fill(self, raw_fields: list[dict], application_fields: list[ApplicationField]) -> FillOutcome:
        outcome = FillOutcome()
        for rf in raw_fields:
            label = rf.get("label") or rf.get("name") or f"field#{rf.get('index')}"
            field_id, confidence = match_field(rf.get("label", ""), rf.get("name", ""))
            app_field = find_field(application_fields, field_id) if field_id else None

            if app_field is not None and app_field.category in SENSITIVE_CATEGORIES and not app_field.auto_fill_allowed:
                decline = _decline_option(rf.get("choices") or [])
                if decline is not None and self._fill_one(rf, decline):
                    outcome.filled.append(label)
                    continue
                if rf.get("required"):
                    outcome.unresolved.append(label)
                continue

            if app_field is None or not app_field.auto_fill_allowed or app_field.category in SENSITIVE_CATEGORIES:
                if rf.get("required"):
                    outcome.unresolved.append(label)
                continue
            if confidence == FieldConfidence.LOW:
                if rf.get("required"):
                    outcome.unresolved.append(label)
                continue

            if rf.get("type") == "file":
                value = app_field.verified_value
                if value and Path(value).exists():
                    if self._upload_one(rf, value):
                        outcome.filled.append(label)
                        outcome.uploads.append(label)
                    else:
                        outcome.unresolved.append(label)
                elif rf.get("required"):
                    outcome.unresolved.append(label)
                continue

            value = app_field.verified_value
            choices = rf.get("choices") or []
            if choices and not any(str(value).strip().lower() == str(c).strip().lower() for c in choices):
                if rf.get("required"):
                    outcome.unresolved.append(label)
                continue

            if self._fill_one(rf, value):
                outcome.filled.append(label)
            elif rf.get("required"):
                outcome.unresolved.append(label)
        return outcome

    def _fill_one(self, rf: dict, value) -> bool:
        try:
            rtype = rf.get("type")
            if rtype in ("radio", "checkbox"):
                self.page.get_by_label(str(value), exact=False).first.check(timeout=5000)
            elif rtype == "select":
                self.page.locator(_selector_for(rf)).select_option(label=str(value), timeout=5000)
            else:
                self.page.locator(_selector_for(rf)).fill(str(value), timeout=5000)
            return True
        except Exception:  # noqa: BLE001 -- one unfillable field must never abort the whole pass
            return False

    def _upload_one(self, rf: dict, path: str) -> bool:
        try:
            self.page.locator(_selector_for(rf)).set_input_files(path, timeout=10000)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _do_advance_step(self) -> dict:
        next_button = _detect_button(self.page, _NEXT_BUTTON_PHRASES, exclude_phrases=_SUBMIT_BUTTON_PHRASES)
        if next_button is None:
            return {"advanced": False, "reason": "no next/continue control found"}
        try:
            if next_button.get("id"):
                self.page.locator(f"#{next_button['id']}").click(timeout=5000)
            else:
                self.page.get_by_text(next_button["text"], exact=False).first.click(timeout=5000)
            self.page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:  # noqa: BLE001 -- a failed click still means "did not advance"
            return {"advanced": False, "reason": "click on next/continue control failed"}
        self.current_step += 1
        return {"advanced": True, "current_step": self.current_step}

    def _do_capture_confirmation(self) -> ConfirmationOutcome:
        current_url = self.page.url
        try:
            text = self.page.inner_text("body")
        except Exception:  # noqa: BLE001
            text = ""
        lowered = text.lower()
        if not any(p in lowered for p in _SUCCESS_PHRASES) and "confirmation" not in lowered:
            return ConfirmationOutcome(confirmed=False, current_url=current_url)
        if not any(p in lowered for p in _SUCCESS_PHRASES):
            return ConfirmationOutcome(confirmed=False, current_url=current_url)
        match = _CONFIRMATION_ID_RE.search(text)
        confirmation_id = match.group(1) if match else ""
        snippet = text.strip()[:300]
        fingerprint = hashlib.sha256(snippet.encode("utf-8")).hexdigest()[:24]
        return ConfirmationOutcome(
            confirmed=True, current_url=current_url, confirmation_id=confirmation_id,
            confirmation_text_fingerprint=fingerprint,
        )

    def _do_close(self) -> None:
        for obj, closer in ((self.context, "close"), (self.browser, "close")):
            try:
                if obj is not None:
                    getattr(obj, closer)()
            except Exception:  # noqa: BLE001 -- best-effort cleanup, never raises
                pass
        try:
            if self._pw_cm is not None:
                self._pw_cm.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass


def _selector_for(rf: dict) -> str:
    if rf.get("id"):
        return f"#{rf['id']}"
    if rf.get("name"):
        return f"[name='{rf['name']}']"
    return f":nth-match(input, textarea, select, {rf.get('index', 0) + 1})"


def _decline_option(choices: list[str]) -> Optional[str]:
    return next(
        (c for c in choices if any(p in c.lower().replace("'", "") for p in DECLINE_TO_SELF_IDENTIFY_PHRASES)),
        None,
    )


def _fingerprint_fields(raw_fields: list[dict]) -> str:
    import json

    signature = [
        {"name": rf.get("name", ""), "label": rf.get("label", ""), "type": rf.get("type", ""),
         "required": bool(rf.get("required")), "choices": sorted(rf.get("choices") or [])}
        for rf in raw_fields
    ]
    normalized = json.dumps(signature, sort_keys=True)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:40]


def _detect_fields(page) -> list[dict]:
    """Real-browser DOM scan: input/textarea/select, plus grouped radio/
    checkbox sets by `name`, with label/aria-label/placeholder/fieldset-legend
    fallback and a required-indicator check (CLAUDE.md Phase 10 section 9).
    Never touches password/hidden/submit/button inputs -- those are always
    left for the human (password) or handled separately (submit)."""
    return page.evaluate(
        """
        () => {
          const results = [];
          const seenGroups = new Set();
          document.querySelectorAll('input, textarea, select').forEach((el, idx) => {
            const type = (el.getAttribute('type') || el.tagName).toLowerCase();
            if (['hidden', 'password', 'submit', 'button', 'image'].includes(type)) return;

            let label = '';
            if (el.labels && el.labels.length) label = el.labels[0].innerText;
            if (!label) label = el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
            if (!label && el.closest('label')) label = el.closest('label').innerText;
            let fieldsetLabel = '';
            const fs = el.closest('fieldset');
            if (fs) {
              const legend = fs.querySelector('legend');
              if (legend) fieldsetLabel = legend.innerText;
            }
            const required = !!(el.required || el.getAttribute('aria-required') === 'true');

            if (type === 'radio' || type === 'checkbox') {
              const groupKey = 'group:' + (el.name || ('idx' + idx));
              if (seenGroups.has(groupKey)) return;
              seenGroups.add(groupKey);
              const group = el.name ? document.querySelectorAll(`input[name="${el.name}"]`) : [el];
              const choices = Array.from(group).map((g) => {
                let l = '';
                if (g.labels && g.labels.length) l = g.labels[0].innerText;
                if (!l && g.closest('label')) l = g.closest('label').innerText;
                return (l || g.value || '').trim();
              }).filter(Boolean);
              // For a radio/checkbox GROUP, the fieldset legend (the actual
              // question, e.g. "Will you now or in the future require
              // sponsorship?") must win over the first option's own
              // per-choice label (e.g. "Yes") -- the opposite priority from
              // a normal single-value field below. A real E2E test against
              // live Chromium caught this: the old (label || fieldsetLabel)
              // order silently mislabeled every radio group as its first
              // choice's text, so match_field() never recognized the
              // question at all.
              results.push({
                index: idx, label: (fieldsetLabel || label || '').trim(), type: type,
                name: el.name || '', id: '', required: required, choices: choices,
              });
              return;
            }

            let choices = [];
            if (el.tagName === 'SELECT') {
              choices = Array.from(el.options).map((o) => o.textContent.trim()).filter(Boolean);
            }
            results.push({
              index: idx, label: (label || fieldsetLabel || '').trim(), type: type,
              name: el.name || '', id: el.id || '', required: required, choices: choices,
            });
          });
          return results;
        }
        """
    )


def _detect_button(page, include_phrases: tuple, exclude_phrases: tuple = ()) -> Optional[dict]:
    return page.evaluate(
        """
        ([includePhrases, excludePhrases]) => {
          const isVisible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
          const candidates = Array.from(document.querySelectorAll('button, input[type=submit], [role="button"]'));
          for (const el of candidates) {
            if (!isVisible(el)) continue;
            const text = ((el.innerText || el.value || '') + '').trim().toLowerCase();
            if (!text) continue;
            if (excludePhrases.some((p) => text.includes(p))) continue;
            if (includePhrases.some((p) => text.includes(p))) {
              return {text: ((el.innerText || el.value || '') + '').trim(), id: el.id || '', name: el.name || ''};
            }
          }
          return null;
        }
        """,
        [list(include_phrases), list(exclude_phrases)],
    )


# --- module-level registry / public API -------------------------------------

_REGISTRY: dict[str, _LiveSession] = {}
_REGISTRY_LOCK = threading.Lock()


def active_count() -> int:
    with _REGISTRY_LOCK:
        return len(_REGISTRY)


def is_live(session_id: str) -> bool:
    with _REGISTRY_LOCK:
        return session_id in _REGISTRY


def open_session(session_id: str, *, provider: str, url: str) -> DiscoveryOutcome:
    """Launches a real (visible unless BROWSER_HEADLESS) browser, navigates to
    `url`, and returns the initial discovery outcome. Raises
    BrowserRuntimeUnavailable / BrowserRuntimeBusy rather than silently
    no-op-ing."""
    _require_available()
    with _REGISTRY_LOCK:
        if len(_REGISTRY) >= max(1, config.BROWSER_ASSIST_CONCURRENCY):
            raise BrowserRuntimeBusy(
                f"BROWSER_ASSIST_CONCURRENCY={config.BROWSER_ASSIST_CONCURRENCY} reached -- "
                "close or finish an existing browser-assist session first."
            )
        live = _LiveSession(session_id, provider, url)
        _REGISTRY[session_id] = live
    try:
        live.run(live._do_open, url, timeout=config.BROWSER_ASSIST_TIMEOUT_SECONDS + 15)
    except Exception:
        _discard(session_id)
        raise
    return rediscover(session_id)


def rediscover(session_id: str) -> DiscoveryOutcome:
    """CLAUDE.md Phase 10 section 8/33: never assumes the page stayed
    unchanged -- always re-scans the live page's CURRENT state."""
    live = _get_live(session_id)
    return live.run(live._do_discover, timeout=30)


def fill_fields(session_id: str, raw_fields: list[dict], application_fields: list[ApplicationField]) -> FillOutcome:
    live = _get_live(session_id)
    return live.run(live._do_fill, raw_fields, application_fields, timeout=60)


def advance_step(session_id: str) -> dict:
    live = _get_live(session_id)
    return live.run(live._do_advance_step, timeout=30)


def capture_confirmation(session_id: str) -> ConfirmationOutcome:
    live = _get_live(session_id)
    return live.run(live._do_capture_confirmation, timeout=15)


def close_session(session_id: str) -> None:
    with _REGISTRY_LOCK:
        live = _REGISTRY.pop(session_id, None)
    if live is None:
        return
    try:
        live.run(live._do_close, timeout=20)
    except Exception:  # noqa: BLE001 -- closing must never raise
        pass
    live.executor.shutdown(wait=False)


def _discard(session_id: str) -> None:
    with _REGISTRY_LOCK:
        live = _REGISTRY.pop(session_id, None)
    if live is not None:
        live.executor.shutdown(wait=False)


def _get_live(session_id: str) -> _LiveSession:
    with _REGISTRY_LOCK:
        live = _REGISTRY.get(session_id)
    if live is None:
        raise BrowserRuntimeUnavailable(
            f"no live browser for session {session_id} in this process -- it either finished, was closed, "
            "or this worker process restarted (see app.applications.browser_assist.resume_session for the "
            "honest crash-recovery fallback)."
        )
    return live
