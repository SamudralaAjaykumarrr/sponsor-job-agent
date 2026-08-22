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
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from app import config
from app.applications.apply_entry import (
    ApplyControlClassification,
    EntryDetectionResult,
    EntryStage,
    StepConfidence,
    classify_apply_control_detailed,
    classify_stage,
    detect_entry_result,
    is_confirmation_page_text,
    is_review_page_text,
    parse_step_progress,
    select_apply_control,
)
from app.applications import spa_events
from app.applications.confirmation_evidence import classify_confirmation_evidence
from app.applications.domain_allowlist import is_allowed_host_for_session
from app.applications.job_identity import (
    IdentityResult,
    JobIdentitySignals,
    JobIdentityVerdict,
    meets_min_confidence,
    verify_job_identity,
    verify_job_identity_full,
)
from app.applications.mapping import match_field
from app.applications.models import ApplicationField, FieldConfidence, SENSITIVE_CATEGORIES
from app.applications import provider_health
from app.applications.schema import DECLINE_TO_SELF_IDENTIFY_PHRASES, find_field
from app.applications.workday_tenant import parse_workday_tenant

# CLAUDE.md Phase 12 sections 14-15: recursive shadow-DOM-piercing query
# helper, INLINED INSIDE every DOM-scanning `page.evaluate` function body in
# this module (Playwright's `evaluate()` requires ONE function expression,
# not a function declaration followed by a separate arrow function -- this
# is a nested declaration, spliced in via simple string concatenation right
# after the arrow function's opening brace, never a second top-level
# statement). Only ever walks OPEN shadow roots -- `el.shadowRoot` is null/
# undefined for a closed one, so this never attempts to bypass browser-
# enforced encapsulation; a closed shadow root's contents simply aren't
# found, which is the correct, honest "unsupported" outcome (CLAUDE.md
# section 62), never a bypass attempt.
_DEEP_QUERY_JS = """
          function __deepQueryAll(root, selector) {
            const out = [];
            const walk = (node) => {
              out.push(...node.querySelectorAll(selector));
              node.querySelectorAll('*').forEach((el) => { if (el.shadowRoot) walk(el.shadowRoot); });
            };
            walk(root);
            return out;
          }
"""


class BrowserRuntimeUnavailable(Exception):
    """Raised when BROWSER_ASSIST_ENABLED is False, or Playwright / its
    browser binaries aren't installed -- never silently swallowed."""


class BrowserRuntimeBusy(Exception):
    """Raised when BROWSER_ASSIST_CONCURRENCY's bound is already reached
    (CLAUDE.md Phase 10 section 45) -- browser sessions are expensive and
    interactive, so this is a hard cap, not a queue-and-wait."""


_NEXT_BUTTON_PHRASES = ("next", "continue", "save and continue", "next step")
# CLAUDE.md Phase 11 sections 4-6: "apply now" was a Phase 10 bug -- it was
# previously listed as a FINAL-submit phrase, which meant a landing-page
# apply-entry control could be misclassified as (never clicked, but also
# never safely navigated past) a final submit action. Apply-entry phrases now
# live only in app.applications.apply_entry.NAVIGATION_SAFE_PHRASES; this
# tuple is FINAL submit text only.
_SUBMIT_BUTTON_PHRASES = ("submit application", "submit your application", "submit", "send application")
_MFA_PHRASES = ("verification code", "authentication code", "two-factor", "2fa", "one-time code", "otp")
_SUCCESS_PHRASES = (
    "thank you for applying", "application received", "application submitted", "successfully applied",
    "we've received your application", "we have received your application",
    "your application has been submitted", "thank you for your application", "thank you -- your application",
)
# CLAUDE.md Phase 11 section 36: "you already applied" is evidence of a
# PRIOR application, not a fresh success -- must never be folded into
# _SUCCESS_PHRASES / a fabricated new CONFIRMED event.
_DUPLICATE_APPLICATION_PHRASES = (
    "you have already applied", "already applied to this position", "already applied for this job",
    "you already applied", "application already submitted", "already submitted an application",
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
    # --- CLAUDE.md Phase 11 sections 4, 18-19, 31 -----------------------
    stage: str = EntryStage.APPLICATION_ENTRY.value
    entry_detection_result: str = EntryDetectionResult.UNSUPPORTED.value
    apply_entry_control: Optional[dict] = None
    current_step_observed: Optional[int] = None
    total_steps_observed: Optional[int] = None
    step_confidence: str = StepConfidence.UNKNOWN.value
    # --- CLAUDE.md Phase 12 sections 14-15, 26-27, 41 --------------------
    iframe_used: bool = False
    shadow_dom_used: bool = False
    iframe_host: str = ""
    render_time_ms: Optional[int] = None


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
    # CLAUDE.md Phase 11 section 36: kept distinct from `confirmed` -- a
    # duplicate-application observation is real evidence, but of a
    # different fact (a submission already exists somewhere), so a caller
    # must branch on this before ever treating `confirmed=False` here as
    # "nothing happened yet".
    already_applied: bool = False
    # CLAUDE.md Phase 13 section 51: the graded ConfirmationEvidenceStrength
    # value (STRONG/MODERATE/WEAK/NONE) behind this outcome -- see
    # app.applications.confirmation_evidence.
    evidence_strength: str = ""


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


def _wait_for_stable_state(page) -> dict:
    """CLAUDE.md Phase 12 sections 10-13: bounded, deterministic wait used
    instead of trusting `wait_for_load_state("networkidle")` alone -- a
    genuinely SPA-rendered page may keep issuing background XHR/websocket
    traffic indefinitely and never reach networkidle at all. Polls, at most
    `BROWSER_DOM_STABILIZATION_TIMEOUT_MS`, for whichever comes first: (a)
    recognizable application content (a password field, or an ordinary
    fillable field), (b) the DOM's own element-count signature settling
    across `BROWSER_DOM_STABILIZATION_SETTLE_POLLS` consecutive polls (the
    page finished whatever it was doing, even if nothing recognizable ever
    appeared -- e.g. a plain job-description landing page with no form), or
    (c) the timeout. Never an arbitrary long sleep -- every poll interval and
    the overall bound are configured, not guessed."""
    timeout_s = config.BROWSER_DOM_STABILIZATION_TIMEOUT_MS / 1000.0
    poll_s = max(0.05, config.BROWSER_DOM_STABILIZATION_POLL_MS / 1000.0)
    settle_target = max(1, config.BROWSER_DOM_STABILIZATION_SETTLE_POLLS)
    start = time.monotonic()
    deadline = start + timeout_s
    last_signature = None
    stable_polls = 0
    while True:
        try:
            has_password = page.locator("input[type=password]").count() > 0
            has_fields = page.locator(
                "input:not([type=hidden]):not([type=password]):not([type=submit]):not([type=button]), "
                "textarea, select"
            ).count() > 0
            signature = page.evaluate("() => document.documentElement.outerHTML.length")
        except Exception:  # noqa: BLE001 -- a page mid-navigation may throw transiently; keep polling
            has_password, has_fields, signature = False, False, None
        if has_password or has_fields:
            return {"reason": "content_ready", "elapsed_ms": int((time.monotonic() - start) * 1000)}
        if signature is not None and signature == last_signature:
            stable_polls += 1
            if stable_polls >= settle_target:
                return {"reason": "dom_stable", "elapsed_ms": int((time.monotonic() - start) * 1000)}
        else:
            stable_polls = 0
        last_signature = signature
        if time.monotonic() >= deadline:
            return {"reason": "timeout", "elapsed_ms": int((time.monotonic() - start) * 1000)}
        time.sleep(poll_s)


def _scan_iframes(page, provider: str, original_url: str) -> dict:
    """CLAUDE.md Phase 12 section 14: enumerates every frame Playwright can
    normally read -- this is the same access a browser's own devtools has
    (Playwright's CDP-backed `Frame.evaluate()`), never a cross-origin
    sandbox bypass. An allowed-host frame's fields are folded into the main
    scan; an UNEXPECTED-host frame only pauses the session when it actually
    contains form-shaped content -- an ad/analytics/tracking iframe (common
    on real career pages, unrelated to the application flow) must never by
    itself trigger a pause."""
    extra_fields: list[dict] = []
    used = False
    unexpected_host = ""
    used_host = ""
    submit_button = None
    next_button = None
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        frame_url = frame.url or ""
        if not frame_url or frame_url == "about:blank":
            continue
        try:
            frame_fields = _detect_fields(frame)
        except Exception:  # noqa: BLE001 -- a detached/navigating frame is just skipped
            frame_fields = []
        if not frame_fields:
            continue
        if is_allowed_host_for_session(provider, original_url, frame_url):
            for f in frame_fields:
                f["_frame"] = frame  # see _LiveSession._fill_one/_upload_one
            extra_fields.extend(frame_fields)
            used = True
            used_host = (urlparse(frame_url).hostname or "").lower()
            # The submit/next control for an in-iframe form lives in the
            # SAME frame, not the main document -- must be located there too
            # (a real live test caught fields being correctly discovered and
            # filled while the button scan silently kept looking only at
            # the top-level page).
            try:
                submit_button = submit_button or _detect_button(frame, _SUBMIT_BUTTON_PHRASES)
                next_button = next_button or _detect_button(
                    frame, _NEXT_BUTTON_PHRASES, exclude_phrases=_SUBMIT_BUTTON_PHRASES,
                )
            except Exception:  # noqa: BLE001
                pass
        else:
            unexpected_host = (urlparse(frame_url).hostname or "").lower()
            break
    return {
        "fields": extra_fields, "used": used, "unexpected_host": unexpected_host, "host": used_host,
        "submit_button": submit_button, "next_button": next_button,
    }


def _tenant_site_for(provider: str, url: str) -> tuple[str, str]:
    """CLAUDE.md Phase 13 section 11: tenant/site is only a meaningful
    concept for tenant-shaped providers (Workday today) -- every other
    provider's health/identity rows carry empty tenant/site, matching
    app.applications.workday_tenant's own 'never fabricate a tenant that
    isn't actually present' rule."""
    if (provider or "").lower() != "workday":
        return "", ""
    info = parse_workday_tenant(url)
    return (info.tenant, info.site) if info.recognized else ("", "")


def _extract_observed_job_meta(page) -> dict:
    """CLAUDE.md Phase 13 sections 4, 8: bounded, deterministic JSON-LD
    extraction -- reads `<script type="application/ld+json">` blocks
    looking for a schema.org JobPosting entry (title/hiringOrganization.name/
    identifier/jobLocation), the same standard, publicly-documented
    mechanism search engines use. Never executes arbitrary page JS beyond a
    single `JSON.parse` of already-embedded, page-authored data -- the same
    risk profile as this module's existing `page.content()`/
    `page.inner_text()` calls. Returns {} when no JobPosting block is
    present; never guesses a substitute from other page text."""
    try:
        data = page.evaluate(
            """
            () => {
              const scripts = Array.from(document.querySelectorAll('script[type="application/ld+json"]'));
              for (const s of scripts) {
                let parsed;
                try { parsed = JSON.parse(s.textContent || '{}'); } catch (e) { continue; }
                const items = Array.isArray(parsed) ? parsed : [parsed];
                for (const item of items) {
                  if (item && item['@type'] === 'JobPosting') {
                    const org = item.hiringOrganization;
                    const ident = item.identifier;
                    let location = '';
                    const loc = item.jobLocation;
                    const locObj = Array.isArray(loc) ? loc[0] : loc;
                    if (locObj && locObj.address) {
                      const addr = locObj.address;
                      location = addr.addressLocality || addr.addressRegion || addr.streetAddress || '';
                    }
                    return {
                      title: item.title || '',
                      company: (org && (org.name || '')) || '',
                      identifier: (ident && (ident.value || (typeof ident === 'string' ? ident : ''))) || '',
                      location: location,
                    };
                  }
                }
              }
              return null;
            }
            """
        )
    except Exception:  # noqa: BLE001 -- a malformed page must never break discovery
        return {}
    return data or {}


def _page_uses_shadow_dom(page) -> bool:
    try:
        return bool(page.evaluate(
            "() => Array.from(document.querySelectorAll('*')).some((el) => !!el.shadowRoot)"
        ))
    except Exception:  # noqa: BLE001
        return False


class _LiveSession:
    def __init__(self, session_id: str, provider: str, application_url: str, *,
                 job_id: Optional[int] = None, expected_title: str = "", expected_company: str = "",
                 expected_location: str = ""):
        self.session_id = session_id
        self.provider = provider
        self.application_url = application_url
        # CLAUDE.md Phase 13 sections 4, 9-10: the job's own stored
        # title/company/location, carried into the live session so
        # `_do_discover` can run the full multi-signal identity check
        # immediately before an upload / final-submit moment without a
        # second DB round-trip.
        self.job_id = job_id
        self.expected_title = expected_title
        self.expected_company = expected_company
        self.expected_location = expected_location
        self.tenant, self.site = _tenant_site_for(provider, application_url)
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
        # CLAUDE.md Phase 12 sections 10-13: a genuinely SPA-rendered page
        # may still be hydrating right after `goto()` returns (the initial
        # HTML is often close to empty) -- wait for either recognizable
        # content or a settled DOM before the first discovery pass, rather
        # than trusting the raw post-navigation snapshot.
        result = _wait_for_stable_state(self.page)
        if result["reason"] == "timeout":
            spa_events.record(spa_events.EVENT_DYNAMIC_FORM_TIMEOUT, session_id=self.session_id,
                               provider=self.provider, duration_ms=result["elapsed_ms"])
        elif result["reason"] == "content_ready":
            spa_events.record(spa_events.EVENT_DYNAMIC_FORM_DETECTED, session_id=self.session_id,
                               provider=self.provider, duration_ms=result["elapsed_ms"])

    def _do_advance_to_route(self, current_url_before: str) -> dict:
        """CLAUDE.md Phase 12 section 10: detects a client-side route change
        (URL changed with no full page navigation -- pushState/hashchange/
        History API) by comparing the URL before and after a bounded
        stabilization wait, used after any click that might trigger one."""
        outcome = _wait_for_stable_state(self.page)
        route_changed = self.page.url != current_url_before
        if route_changed:
            spa_events.record(spa_events.EVENT_SPA_ROUTE_DETECTED, session_id=self.session_id,
                               provider=self.provider, detail=f"{current_url_before} -> {self.page.url}")
        return {"route_changed": route_changed, **outcome}

    def _do_discover(self) -> DiscoveryOutcome:
        page = self.page
        current_url = page.url
        if not is_allowed_host_for_session(self.provider, self.application_url, current_url):
            return DiscoveryOutcome(pause_reason="PLATFORM_POLICY_RESTRICTED", current_url=current_url)

        content_lower = page.content().lower()
        try:
            body_text = page.inner_text("body")
        except Exception:  # noqa: BLE001
            body_text = ""

        # CLAUDE.md Phase 13 sections 6, 17-20: a bounded LIVE canary run
        # against real, current Greenhouse/Lever/Ashby/Workable postings
        # (scripts/phase13_live_validation.py) caught a real false positive
        # here -- these providers' real pages now defensively load a
        # reCAPTCHA library SCRIPT TAG (invisible v3 verification) on every
        # visit, with no challenge ever rendered to anyone. The old bare
        # `"captcha" in content_lower` check matched that script's own src
        # URL text, pausing every ordinary visit as CAPTCHA_PRESENT. The
        # three DOM-ELEMENT checks below (an actual captcha-classed/id'd
        # element, or a captcha-src iframe -- i.e. a widget that would
        # actually be VISIBLE to a person) are the correct signal, and
        # already catch the real E2E fixture (tests/browser_fixtures.py's
        # `<div class="g-recaptcha">`, since "recaptcha" contains "captcha"
        # as a substring) -- removing the raw whole-page-text check is a
        # pure precision improvement, not a loosened safety boundary: a
        # genuinely rendered SmartRecruiters/DataDome-style challenge still
        # trips this via its own challenge iframe/class, and this project
        # never attempts to solve or bypass one either way.
        has_captcha = (
            page.locator("iframe[src*='captcha' i]").count() > 0
            or page.locator("[class*='captcha' i]").count() > 0
            or page.locator("[id*='captcha' i]").count() > 0
        )
        if has_captcha:
            provider_health.record_failure(self.provider, provider_health.FailureKind.CAPTCHA,
                                            tenant=self.tenant, site=self.site)
            return DiscoveryOutcome(pause_reason="CAPTCHA_PRESENT", current_url=current_url)

        login_wall = page.locator("input[type=password]").count() > 0
        if login_wall:
            provider_health.record_failure(self.provider, provider_health.FailureKind.AUTH_GATE,
                                            tenant=self.tenant, site=self.site)
            if any(p in content_lower for p in _MFA_PHRASES):
                return DiscoveryOutcome(pause_reason="MFA_REQUIRED", current_url=current_url)
            return DiscoveryOutcome(pause_reason="LOGIN_REQUIRED", current_url=current_url)

        start = time.monotonic()
        raw_fields = _detect_fields(page)
        submit_button = _detect_button(page, _SUBMIT_BUTTON_PHRASES)
        next_button = _detect_button(page, _NEXT_BUTTON_PHRASES, exclude_phrases=_SUBMIT_BUTTON_PHRASES)

        # CLAUDE.md Phase 12 section 14: an unexpected-host iframe that
        # actually contains form-shaped content pauses for review; an
        # allowed-host iframe's fields are merged into the main-document
        # scan (a real ATS may mount its whole application form inside a
        # same-origin iframe wrapper).
        iframe_scan = _scan_iframes(page, self.provider, self.application_url)
        if iframe_scan["unexpected_host"]:
            spa_events.record(spa_events.EVENT_IFRAME_UNEXPECTED_HOST, session_id=self.session_id,
                               provider=self.provider, detail=iframe_scan["unexpected_host"])
            return DiscoveryOutcome(pause_reason="IFRAME_UNEXPECTED_HOST", current_url=current_url)
        if iframe_scan["used"]:
            raw_fields = raw_fields + iframe_scan["fields"]
            submit_button = submit_button or iframe_scan.get("submit_button")
            next_button = next_button or iframe_scan.get("next_button")
            spa_events.record(spa_events.EVENT_IFRAME_FORM_DETECTED, session_id=self.session_id,
                               provider=self.provider, result=str(len(iframe_scan["fields"])))

        shadow_used = _page_uses_shadow_dom(page)
        if shadow_used and raw_fields:
            spa_events.record(spa_events.EVENT_SHADOW_FORM_DETECTED, session_id=self.session_id,
                               provider=self.provider)

        fingerprint = _fingerprint_fields(raw_fields)

        current_host = (urlparse(current_url).hostname or "").lower()
        apply_control = _detect_apply_entry_control(page, current_host)
        if apply_control is not None and apply_control.get("ambiguous"):
            spa_events.record(spa_events.EVENT_APPLY_CONTROL_UNKNOWN, session_id=self.session_id,
                               provider=self.provider, detail=apply_control.get("reason", ""))
            return DiscoveryOutcome(pause_reason="AMBIGUOUS_APPLY_CONTROL", current_url=current_url)
        control_classification = (
            ApplyControlClassification(apply_control["classification"]) if apply_control else None
        )
        if apply_control is not None:
            if apply_control.get("redirect_trust") == "TRUSTED_ATS_REDIRECT":
                event = spa_events.EVENT_TRUSTED_REDIRECT
            elif apply_control.get("redirect_trust") == "UNTRUSTED":
                event = spa_events.EVENT_BLOCKED_REDIRECT
            else:
                event = spa_events.EVENT_APPLY_CONTROL_DETECTED
            spa_events.record(event, session_id=self.session_id, provider=self.provider,
                               result=apply_control["classification"], detail=apply_control.get("reason", ""))

        # CLAUDE.md Phase 11 section 4: a page is only ever a review/
        # confirmation stage, never conflated with a plain form page even if
        # it happens to also contain fields.
        is_review = is_review_page_text(body_text)
        is_confirmation = is_confirmation_page_text(body_text)
        stage = classify_stage(
            has_form_fields=bool(raw_fields), has_apply_control=apply_control is not None,
            is_review_page=is_review, is_confirmation_page=is_confirmation,
        )

        # CLAUDE.md Phase 12 section 38: verify the page we're about to fill
        # still corresponds to the job this session was opened for -- only a
        # CONFIDENTLY extracted requisition-token mismatch stops the flow
        # (never a guess; see app.applications.job_identity).
        if stage == EntryStage.APPLICATION_FORM:
            identity = verify_job_identity(self.application_url, current_url)
            if identity.result == IdentityResult.MISMATCH:
                spa_events.record(spa_events.EVENT_JOB_IDENTITY_MISMATCH, session_id=self.session_id,
                                   provider=self.provider, detail=identity.reason)
                return DiscoveryOutcome(pause_reason="JOB_IDENTITY_MISMATCH", current_url=current_url)

        # CLAUDE.md Phase 13 sections 4, 9-10 (acceptance correction): a
        # formal, multi-signal identity recheck immediately before the two
        # highest-stakes moments -- a resume upload (a file-type field is
        # about to be filled) or READY_FOR_FINAL_SUBMIT (a submit control
        # was just found). Only a VERIFIED verdict may continue unattended.
        # MISMATCH (a confirmed contradiction) and PROBABLE/AMBIGUOUS/
        # INSUFFICIENT (not enough independent evidence to proceed
        # unattended, even though nothing was confirmed wrong) both stop the
        # flow -- they are recorded as DISTINCT pause reasons/statuses
        # (JOB_IDENTITY_MISMATCH vs JOB_IDENTITY_UNVERIFIED) so a human/
        # doctor/dashboard can tell "this looks like the wrong job" apart
        # from "we simply could not confirm this is the right job", but
        # neither is ever treated as safe to continue past unattended.
        has_upload_field = any(f.get("type") == "file" for f in raw_fields)
        # Scoped to a genuine APPLICATION_FORM/FINAL_REVIEW stage -- a
        # landing-page lookalike whose only control merely reads "Submit
        # Application" (CLAUDE.md Phase 11 sections 5-6's FINAL_SUBMIT-
        # lookalike case) has no real form/fields at all and must never
        # trigger this gate; `apply_entry.py`'s own apply-entry safety
        # already handles that page never being auto-clicked.
        is_form_or_review_stage = stage in (EntryStage.APPLICATION_FORM, EntryStage.FINAL_REVIEW)
        if config.APPLICATION_IDENTITY_REQUIRED and is_form_or_review_stage and (
            has_upload_field or submit_button is not None
        ):
            observed_meta = _extract_observed_job_meta(page)
            # Provider is never compared here -- both sides would trivially
            # be `self.provider` (this session's own belief, not an
            # independent observation of the current page), which would
            # inflate confidence without genuine evidence. Tenant/site ARE
            # independently re-derived from the CURRENT url (never the
            # fixed self.tenant/self.site captured at session-open time),
            # so a genuine tenant/site drift is a real comparable signal.
            observed_tenant, observed_site = _tenant_site_for(self.provider, current_url)
            observed_signals = JobIdentitySignals(
                title=observed_meta.get("title", ""), company=observed_meta.get("company", ""),
                tenant=observed_tenant, site=observed_site, url=current_url,
                requisition_id=observed_meta.get("identifier", ""), location=observed_meta.get("location", ""),
            )
            stored_signals = JobIdentitySignals(
                title=self.expected_title, company=self.expected_company, location=self.expected_location,
                tenant=self.tenant, site=self.site, url=self.application_url,
            )
            full_check = verify_job_identity_full(stored_signals, observed_signals)
            check_stage = "PRE_UPLOAD" if has_upload_field else "PRE_FINAL_SUBMIT"
            if self.job_id is not None:
                from app.applications import job_identity as _job_identity
                _job_identity.record_verification(
                    self.job_id, stage=check_stage, stored=stored_signals, observed=observed_signals,
                    verification=full_check, session_id=self.session_id,
                )
            if full_check.verdict == JobIdentityVerdict.MISMATCH:
                spa_events.record(spa_events.EVENT_JOB_IDENTITY_MISMATCH, session_id=self.session_id,
                                   provider=self.provider, stage=check_stage, detail=full_check.reason)
                return DiscoveryOutcome(pause_reason="JOB_IDENTITY_MISMATCH", current_url=current_url)
            if not meets_min_confidence(full_check.verdict, config.APPLICATION_IDENTITY_MIN_CONFIDENCE):
                # PROBABLE / AMBIGUOUS / INSUFFICIENT (by default) -- never
                # confirmed wrong, but never confidently confirmed right
                # either. CLAUDE.md Phase 13 acceptance correction: none of
                # these may continue unattended past an upload/final-submit
                # gate unless an operator has explicitly LOWERED
                # APPLICATION_IDENTITY_MIN_CONFIDENCE below its "VERIFIED"
                # default -- a deliberate, documented risk acceptance.
                spa_events.record(spa_events.EVENT_JOB_IDENTITY_UNVERIFIED, session_id=self.session_id,
                                   provider=self.provider, stage=check_stage,
                                   result=full_check.verdict.value, detail=full_check.reason)
                return DiscoveryOutcome(pause_reason="JOB_IDENTITY_UNVERIFIED", current_url=current_url)

        if raw_fields:
            provider_health.record_success(self.provider, tenant=self.tenant, site=self.site,
                                            form_fingerprint=fingerprint)

        entry_result = detect_entry_result(
            has_apply_control=apply_control is not None, apply_control_classification=control_classification,
            has_form_fields=bool(raw_fields), login_wall_present=login_wall,
        )
        current_step, total_steps, step_confidence = parse_step_progress(body_text)
        if current_step is None and next_button:
            # No genuinely parsed indicator, but we DID observe a same-
            # session Next control -- CLAUDE.md Phase 11 section 19: this is
            # an INFERRED signal ("at least one more step exists"), never
            # promoted to EXACT and never given a fabricated total.
            current_step, step_confidence = self.current_step, StepConfidence.INFERRED

        return DiscoveryOutcome(
            pause_reason=None, current_url=current_url, fields=raw_fields, fingerprint=fingerprint,
            submit_button=submit_button, next_button=next_button,
            total_steps_hint=2 if next_button else 1,
            stage=stage.value, entry_detection_result=entry_result.value, apply_entry_control=apply_control,
            current_step_observed=current_step, total_steps_observed=total_steps,
            step_confidence=step_confidence.value, iframe_used=iframe_scan["used"], shadow_dom_used=shadow_used,
            iframe_host=iframe_scan.get("host", ""), render_time_ms=int((time.monotonic() - start) * 1000),
        )

    def _do_fill(self, raw_fields: list[dict], application_fields: list[ApplicationField]) -> FillOutcome:
        outcome = self._fill_pass(raw_fields, application_fields)

        # CLAUDE.md Phase 11 section 22: conditional-question rediscovery.
        # Changing a radio/checkbox answer can reveal a field that didn't
        # exist in the DOM at all when raw_fields was first scanned (a
        # genuinely NEW node, not merely an unhidden one -- an
        # already-present-but-hidden field is filled fine by the pass
        # above, since Playwright waits for it to become actionable once
        # the change event runs). One rescan, filled once, never looped --
        # a field that reveals ANOTHER field behind it is a legitimately
        # more elaborate form than this safe, bounded pass supports; it
        # surfaces as an ordinary unresolved-required-field pause instead.
        rescanned = _detect_fields(self.page)
        known_keys = {_field_key(rf) for rf in raw_fields}
        new_fields = [rf for rf in rescanned if _field_key(rf) not in known_keys]
        if new_fields:
            extra = self._fill_pass(new_fields, application_fields)
            outcome.filled.extend(extra.filled)
            outcome.unresolved.extend(extra.unresolved)
            outcome.uploads.extend(extra.uploads)
        return outcome

    def _fill_pass(self, raw_fields: list[dict], application_fields: list[ApplicationField]) -> FillOutcome:
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
        # CLAUDE.md Phase 12 section 14: a field discovered inside an
        # allowed-host iframe is tagged with its own Frame object by
        # `_scan_iframes` -- filling it must target THAT frame, not the main
        # page (a real live test caught this: the field was correctly
        # discovered but every fill attempt silently failed since
        # `self.page.locator()` only ever searches the top-level document).
        target = rf.get("_frame") or self.page
        try:
            rtype = rf.get("type")
            if rtype in ("radio", "checkbox"):
                target.get_by_label(str(value), exact=False).first.check(timeout=5000)
            elif rtype == "select":
                target.locator(_selector_for(rf)).select_option(label=str(value), timeout=5000)
            else:
                target.locator(_selector_for(rf)).fill(str(value), timeout=5000)
            return True
        except Exception:  # noqa: BLE001 -- one unfillable field must never abort the whole pass
            return False

    def _upload_one(self, rf: dict, path: str) -> bool:
        target = rf.get("_frame") or self.page
        try:
            target.locator(_selector_for(rf)).set_input_files(path, timeout=10000)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _do_advance_apply_entry(self) -> dict:
        """CLAUDE.md Phase 11 sections 4-6: the ONLY navigation this module
        performs before form discovery. Re-derives the current page's
        apply-entry control fresh (never trusts a stale one from a previous
        discovery call) and refuses to click anything not freshly
        classified NAVIGATION_SAFE -- a FINAL_SUBMIT/LOGIN_TRIGGER/
        EXTERNAL_REDIRECT/UNKNOWN control is never clicked here, full stop."""
        current_host = (urlparse(self.page.url).hostname or "").lower()
        control = _detect_apply_entry_control(self.page, current_host)
        if control is None or control.get("ambiguous") \
                or control.get("classification") != ApplyControlClassification.NAVIGATION_SAFE.value:
            # CLAUDE.md Phase 12 section 35: apply-entry click-through is
            # only ever attempted against a control FRESHLY classified
            # NAVIGATION_SAFE in THIS call -- once the form is already
            # visible, the caller (app.applications.browser_assist) stops
            # calling this at all (entry_result flips to
            # FORM_ALREADY_VISIBLE), so a genuinely already-applied session
            # never re-clicks and never creates a duplicate route/modal.
            return {"advanced": False, "reason": "no NAVIGATION_SAFE apply-entry control found"}
        before_url = self.page.url
        try:
            if control.get("id"):
                self.page.locator(f"#{control['id']}").click(timeout=5000)
            else:
                self.page.get_by_text(control["text"], exact=False).first.click(timeout=5000)
        except Exception:  # noqa: BLE001 -- a failed click still means "did not advance"
            return {"advanced": False, "reason": "click on apply-entry control failed"}
        # CLAUDE.md Phase 12 sections 10-13: bounded DOM-stabilization wait
        # instead of blind `networkidle` -- a genuinely SPA-rendered apply
        # form (client-side route change, no full page load) may never
        # reach networkidle at all.
        route = self._do_advance_to_route(before_url)
        return {"advanced": True, **route}

    def _do_advance_step(self) -> dict:
        next_button = _detect_button(self.page, _NEXT_BUTTON_PHRASES, exclude_phrases=_SUBMIT_BUTTON_PHRASES)
        if next_button is None:
            return {"advanced": False, "reason": "no next/continue control found"}
        before_url = self.page.url
        try:
            if next_button.get("id"):
                self.page.locator(f"#{next_button['id']}").click(timeout=5000)
            else:
                self.page.get_by_text(next_button["text"], exact=False).first.click(timeout=5000)
        except Exception:  # noqa: BLE001 -- a failed click still means "did not advance"
            return {"advanced": False, "reason": "click on next/continue control failed"}
        self._do_advance_to_route(before_url)
        self.current_step += 1
        return {"advanced": True, "current_step": self.current_step}

    def _do_capture_confirmation(self) -> ConfirmationOutcome:
        current_url = self.page.url
        try:
            text = self.page.inner_text("body")
        except Exception:  # noqa: BLE001
            text = ""
        lowered = text.lower()
        # CLAUDE.md Phase 11 section 36: "you already applied" is evidence
        # of a PRIOR application, checked before (and returned distinctly
        # from) the ordinary success-phrase match below -- it must never be
        # folded into a fresh `confirmed=True` event.
        if any(p in lowered for p in _DUPLICATE_APPLICATION_PHRASES):
            return ConfirmationOutcome(confirmed=False, current_url=current_url, already_applied=True)
        # CLAUDE.md Phase 11 section 35: a real success PHRASE match is
        # required -- text that merely mentions "confirmation" in passing
        # (e.g. "Submit your application to receive confirmation") must
        # never count. `_SUCCESS_PHRASES` are all deliberately affirmative,
        # completed-action phrases ("thank you for applying"), never a bare
        # noun like "confirmation" alone.
        phrase_matched = any(p in lowered for p in _SUCCESS_PHRASES)
        match = _CONFIRMATION_ID_RE.search(text)
        confirmation_id = match.group(1) if match else ""
        # CLAUDE.md Phase 13 sections 49-51: grade the evidence STRENGTH
        # before deciding `confirmed` -- only STRONG/MODERATE evidence may
        # ever confirm (see ConfirmationGrade.confirms()); a lone
        # confirmation-shaped URL/id with no trusted phrase match is WEAK
        # and never confirms on its own, matching the existing
        # phrase-required behavior exactly (no functional change, now
        # explicitly graded and recorded).
        grade = classify_confirmation_evidence(
            phrase_matched=phrase_matched, confirmation_id=confirmation_id, current_url=current_url,
        )
        if not grade.confirms():
            return ConfirmationOutcome(confirmed=False, current_url=current_url,
                                        evidence_strength=grade.strength.value)
        snippet = text.strip()[:300]
        fingerprint = hashlib.sha256(snippet.encode("utf-8")).hexdigest()[:24]
        return ConfirmationOutcome(
            confirmed=True, current_url=current_url, confirmation_id=confirmation_id,
            confirmation_text_fingerprint=fingerprint, evidence_strength=grade.strength.value,
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


def _field_key(rf: dict) -> tuple:
    """Identity key for conditional-question rediscovery (CLAUDE.md Phase 11
    section 22) -- name/id/type rather than `index`, since a newly-inserted
    DOM node shifts every subsequent element's index."""
    return (rf.get("name", ""), rf.get("id", ""), rf.get("type", ""))


def _detect_apply_entry_control(page, current_host: str) -> Optional[dict]:
    """Real-DOM apply-entry candidate scan (CLAUDE.md Phase 11 sections 4, 8;
    Phase 12 sections 5-6, 36-37): looks at every visible button/link/
    role=button (including inside open shadow roots -- CLAUDE.md section 15),
    falling back to aria-label/aria-labelledby text for icon-only controls
    that have no visible innerText, classifies each via
    app.applications.apply_entry.classify_apply_control_detailed (the SAME
    deterministic table browser_assist/tests use, now redirect-trust-aware),
    and resolves the best candidate via app.applications.apply_entry.
    select_apply_control -- multiple NAVIGATION_SAFE candidates pointing at
    genuinely different destinations come back as `{"ambiguous": True, ...}`
    rather than a guessed pick. A FINAL_SUBMIT or UNKNOWN-classified control
    is never returned as the chosen control -- this function's only job is
    finding a safe pre-form navigation target, not enumerating every button
    on the page."""
    candidates = page.evaluate(
        """
        () => {"""
        + _DEEP_QUERY_JS +
        """
          const isVisible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
          const els = __deepQueryAll(document, 'a, button, input[type=submit], input[type=button], [role="button"]');
          return els.filter(isVisible).map((el) => {
            const ariaLabel = el.getAttribute('aria-label') || '';
            let text = ((el.innerText || el.value || '') + '').trim();
            if (!text && ariaLabel) text = ariaLabel.trim();
            if (!text) {
              const labelledBy = el.getAttribute('aria-labelledby');
              if (labelledBy) {
                const labelEl = document.getElementById(labelledBy);
                if (labelEl) text = (labelEl.innerText || '').trim();
              }
            }
            return {
              text: text,
              href: el.tagName === 'A' ? (el.getAttribute('href') || '') : '',
              id: el.id || '',
            };
          }).filter((c) => c.text);
        }
        """
    )
    annotated = []
    for c in candidates:
        detail = classify_apply_control_detailed(c["text"], href=c["href"], current_host=current_host)
        annotated.append({
            "text": c["text"], "href": c["href"], "id": c["id"], "classification": detail.classification.value,
            "reason": detail.reason, "redirect_trust": detail.redirect_trust.value if detail.redirect_trust else "",
        })
    best, ambiguous_reason = select_apply_control(annotated)
    if best is None and ambiguous_reason:
        return {"ambiguous": True, "reason": ambiguous_reason}
    return best


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
    Pierces OPEN shadow roots (CLAUDE.md Phase 12 sections 13, 15, 60-62) via
    the shared `_DEEP_QUERY_JS` helper -- a closed shadow root's contents
    simply aren't found, the correct honest outcome rather than a bypass
    attempt. Never touches password/hidden/submit/button inputs -- those are
    always left for the human (password) or handled separately (submit).
    `page` may also be a Playwright Frame -- both expose `.evaluate()`
    identically, so this same function scans an allowed-host iframe's
    document too (CLAUDE.md Phase 12 section 14)."""
    return page.evaluate(
        """
        () => {"""
        + _DEEP_QUERY_JS +
        """
          const results = [];
          const seenGroups = new Set();
          __deepQueryAll(document, 'input, textarea, select').forEach((el, idx) => {
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
              const group = el.name ? __deepQueryAll(document, `input[name="${el.name}"]`) : [el];
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
        ([includePhrases, excludePhrases]) => {"""
        + _DEEP_QUERY_JS +
        """
          const isVisible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
          const candidates = __deepQueryAll(document, 'button, input[type=submit], [role="button"]');
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


def open_session(session_id: str, *, provider: str, url: str, job_id: Optional[int] = None,
                  expected_title: str = "", expected_company: str = "",
                  expected_location: str = "") -> DiscoveryOutcome:
    """Launches a real (visible unless BROWSER_HEADLESS) browser, navigates to
    `url`, and returns the initial discovery outcome. Raises
    BrowserRuntimeUnavailable / BrowserRuntimeBusy rather than silently
    no-op-ing. `job_id`/`expected_title`/`expected_company`/`expected_location`
    are optional and feed the Phase 13 pre-upload/pre-final-submit job-
    identity recheck (see `_do_discover`) -- CLAUDE.md Phase 13 acceptance
    correction: omitting them means the recheck has NOTHING to compare
    (INSUFFICIENT), and INSUFFICIENT now BLOCKS unattended continuation past
    an upload/final-submit gate exactly like every other non-VERIFIED
    verdict, so a caller that wants unattended continuation past that gate
    MUST supply real, verified title/company for the job."""
    _require_available()
    with _REGISTRY_LOCK:
        if len(_REGISTRY) >= max(1, config.BROWSER_ASSIST_CONCURRENCY):
            raise BrowserRuntimeBusy(
                f"BROWSER_ASSIST_CONCURRENCY={config.BROWSER_ASSIST_CONCURRENCY} reached -- "
                "close or finish an existing browser-assist session first."
            )
        live = _LiveSession(session_id, provider, url, job_id=job_id, expected_title=expected_title,
                             expected_company=expected_company, expected_location=expected_location)
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


def advance_apply_entry(session_id: str) -> dict:
    """CLAUDE.md Phase 11 sections 4-8: clicks the current page's
    NAVIGATION_SAFE apply-entry control (never anything else) to move from a
    landing/apply-entry page toward the real application form. Distinct from
    advance_step(), which clicks a Next/Continue control ON an in-progress
    multi-step form."""
    live = _get_live(session_id)
    return live.run(live._do_advance_apply_entry, timeout=30)


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
