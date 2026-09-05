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
from app.applications import spa_events, workday_tenant
from app.applications.confirmation_evidence import classify_confirmation_evidence
from app.applications.confirmation_parser import parse_confirmation_text
from app.applications.domain_allowlist import is_allowed_host_for_session
from app.applications.dynamic_validation import AdvanceAttempt, AdvanceOutcome, classify_advance_attempt
from app.applications.job_identity import (
    IdentityResult,
    JobIdentitySignals,
    JobIdentityVerdict,
    meets_min_confidence,
    verify_job_identity,
    verify_job_identity_full,
)
from app.applications.mapping import match_field, match_field_with_application_fields
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


# Autonomous-UX-reliability follow-up (2026-08-28): a real employer career
# page commonly carries page CHROME -- a sitewide header search box, a nav
# landmark -- that has nothing to do with the application itself. Before this
# fix, `_detect_fields()` scanned the ENTIRE document indiscriminately, so a
# single unrelated `<input type="search">` sitting in a `<header>` was enough
# to make `has_form_fields` True, which made `detect_entry_result()` report
# FORM_ALREADY_VISIBLE instead of ENTRY_READY -- browser_assist.start_session
# only clicks the apply-entry control on ENTRY_READY (see
# app.applications.browser_assist around its `advance_apply_entry` call), so
# the real "Apply Now" control was never clicked at all. This selector
# excludes the two general, provider-agnostic signals for "this is page
# chrome, not application content": the semantic `<header>`/`<nav>`/
# `role="search|banner|navigation"` landmarks, and the `search` input type
# itself. It is intentionally NOT Airbnb-specific -- it applies to every
# provider's field scan. Only ever used to decide which elements COUNT as
# application form fields; it never affects apply-entry-control (button/link)
# detection, which legitimately can appear inside a sticky/nav-wrapped
# "Apply" button (see `select_apply_control`'s own docstring).
_CHROME_LANDMARK_SELECTOR = 'header, nav, [role="search"], [role="banner"], [role="navigation"]'

_NEXT_BUTTON_PHRASES = ("next", "continue", "save and continue", "next step")
# CLAUDE.md Phase 11 sections 4-6: "apply now" was a Phase 10 bug -- it was
# previously listed as a FINAL-submit phrase, which meant a landing-page
# apply-entry control could be misclassified as (never clicked, but also
# never safely navigated past) a final submit action. Apply-entry phrases now
# live only in app.applications.apply_entry.NAVIGATION_SAFE_PHRASES; this
# tuple is FINAL submit text only.
_SUBMIT_BUTTON_PHRASES = ("submit application", "submit your application", "submit", "send application")
_MFA_PHRASES = ("verification code", "authentication code", "two-factor", "2fa", "one-time code", "otp")
# Real Provider Execution V1: the success/duplicate/confirmation-id tables
# that used to live here are now owned by
# `app.applications.confirmation_parser` -- the SINGLE source, so the exact
# production phrase tables can be exercised against a local fixture's text
# without a live browser. This module never maintains a second, parallel
# copy (the same rule Phase 11 established for apply-entry phrases).


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
    # Real Provider Execution V1: one entry per file that was ACTUALLY
    # accepted by a real form field, carrying enough to bind the artifact
    # durably (see app.applications.document_binding). The DB write itself
    # deliberately happens in `app.applications.browser_assist` -- the
    # orchestration layer that owns session/execution identity -- rather
    # than here, so this module stays a DOM engine.
    upload_details: list[dict] = field(default_factory=list)


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


def _wait_for_stable_state(page, *, provider: str = "", original_url: str = "") -> dict:
    """CLAUDE.md Phase 12 sections 10-13: bounded, deterministic wait used
    instead of trusting `wait_for_load_state("networkidle")` alone -- a
    genuinely SPA-rendered page may keep issuing background XHR/websocket
    traffic indefinitely and never reach networkidle at all. Polls, at most
    `BROWSER_DOM_STABILIZATION_TIMEOUT_MS`, for whichever comes first: (a)
    recognizable application content (a password field, or an ordinary
    fillable field) on the top-level page OR inside an allowed-host iframe,
    (b) the DOM's own element-count signature settling across
    `BROWSER_DOM_STABILIZATION_SETTLE_POLLS` consecutive polls (the page
    finished whatever it was doing, even if nothing recognizable ever
    appeared -- e.g. a plain job-description landing page with no form), or
    (c) the timeout. Never an arbitrary long sleep -- every poll interval and
    the overall bound are configured, not guessed.

    Autonomous-UX-reliability follow-up (2026-08-28, live-caught against a
    real Airbnb/Greenhouse posting): when `provider` is supplied, this ALSO
    treats a genuinely mounted, allowed-host iframe (per
    app.applications.domain_allowlist -- the SAME evidence the post-
    navigation PLATFORM_POLICY_RESTRICTED check already trusts, never a new
    or broader one) with recognizable form content as 'content_ready',
    exactly like a top-level field. Some real ATS embeds (Greenhouse's
    job-boards.greenhouse.io pattern) mount their entire application form
    inside a same-page iframe that is often still loading right after an
    Apply click -- the TOP-level document's own DOM can genuinely finish
    settling (nothing further changes there) well before that cross-document
    iframe has rendered anything, so the checks below (which, without this
    addition, only ever look at `page` itself) can -- and did, live --
    declare stability while the real form was still on its way in.

    Embedded-form-discovery-hardening follow-up (2026-08-28): the
    outerHTML-length "signature" used for (b) only detects the iframe TAG's
    own MARKUP changing -- it says nothing about whether that iframe's own
    (separate) document has actually rendered anything yet, and a real,
    live-observed Greenhouse embed can have its `src` attribute present from
    the very first paint (no markup ever changes) while the cross-origin
    document behind it still takes real, variable, sometimes multi-second
    wall-clock time to load (network + a reCAPTCHA Enterprise widget it
    embeds) -- an earlier version of this fix only caught the DIFFERENT
    "src assigned by a later setTimeout" shape and still declared
    "dom_stable" via the unchanged-signature path in this shape, well before
    that real content ever arrived. The general condition this function
    needs is therefore: "does any <iframe> tag on the page currently point
    (whether via an as-yet-unset/`about:blank` placeholder src, a same-page-
    relative src, or an absolute src) at a destination this session already
    trusts, without a matching live frame yet showing fillable content?" --
    computed via the exact same `app.applications.domain_allowlist.
    is_allowed_host_for_session` evidence table the post-navigation gate
    uses (same-origin OR a known ATS vendor suffix), never a new or broader
    one, and using the browser's OWN `URL(src, document.baseURI)` resolution
    so a relative src is judged by its genuine resolved destination rather
    than compared as a bare string. While that condition holds, (b) never
    declares stability -- this function instead keeps polling, still bounded
    by the same overall `BROWSER_DOM_STABILIZATION_TIMEOUT_MS`, until either
    that iframe resolves to trusted, fillable content (satisfying (a) above)
    or the timeout is reached -- a genuinely untrusted iframe (an ad/
    tracking embed pointed at an unrelated host) never holds the wait open,
    so this can never turn into an unbounded wait for irrelevant content.

    A HIDDEN iframe (a "switch to application form" tab-panel not yet
    revealed, `display:none` or zero-sized) is deliberately excluded from
    BOTH sides of this: not from (a) -- a real, live-caught case showed
    Chromium can eagerly load a hidden iframe's content, and treating that
    as "ready" would skip the apply-entry click that actually reveals it and
    then fail every fill attempt (`Locator.fill()` requires visibility) --
    and not from the "pending" condition either, since no amount of extra
    waiting makes a click-gated iframe visible; this function instead lets
    (b) resolve quickly so the caller's own apply-entry-click logic gets a
    chance to reveal it, at which point the next poll re-evaluates
    naturally. Omitting `provider`, or finding no such pending/ready iframe,
    leaves this function's behavior byte-for-byte unchanged."""
    from app.applications.domain_allowlist import is_allowed_host_for_session

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
            iframe_infos = page.evaluate(
                """
                () => Array.from(document.querySelectorAll('iframe')).map((f) => {
                  const raw = f.getAttribute('src') || '';
                  const src = (!raw || raw === 'about:blank') ? '' : (() => {
                    try { return new URL(raw, document.baseURI).href; } catch (e) { return raw; }
                  })();
                  return {src: src, visible: !!(f.offsetWidth || f.offsetHeight || f.getClientRects().length)};
                })
                """
            ) if provider else []
        except Exception:  # noqa: BLE001 -- a page mid-navigation may throw transiently; keep polling
            has_password, has_fields, signature, iframe_infos = False, False, None, []
        has_pending_iframe = False
        if not (has_password or has_fields) and provider:
            for frame in page.frames:
                if frame == page.main_frame:
                    continue
                try:
                    frame_url = frame.url
                    if not frame_url or frame_url == "about:blank" \
                            or not is_allowed_host_for_session(provider, original_url, frame_url):
                        continue
                    # CLAUDE.md embedded-form-discovery-hardening: a frame
                    # can be fully loaded with real fields while its OWNING
                    # <iframe> element is still hidden (e.g. a "switch to
                    # application form" tab-panel not yet revealed) -- a
                    # real, live-caught case, since Chromium may eagerly
                    # fetch a hidden iframe's content. `Locator.fill()`
                    # requires visibility/actionability, so treating hidden
                    # content as "ready" here would skip the click that
                    # reveals it and then fail to fill anything. Only a
                    # frame whose element is genuinely visible counts.
                    frame_element = frame.frame_element()
                    if not frame_element.is_visible():
                        continue
                    frame_has_content = (
                        frame.locator("input[type=password]").count() > 0
                        or frame.locator(
                            "input:not([type=hidden]):not([type=password]):not([type=submit])"
                            ":not([type=button]), textarea, select"
                        ).count() > 0
                    )
                except Exception:  # noqa: BLE001 -- a still-loading/detaching frame may throw transiently
                    frame_has_content = False
                if frame_has_content:
                    has_fields = True
                    break
            if not has_fields:
                for info in iframe_infos:
                    # A HIDDEN iframe is never treated as "pending" -- no
                    # amount of waiting reveals it; that is the apply-entry
                    # click's job, which this only needs to get out of the
                    # way for quickly (see the frame_element.is_visible()
                    # check above).
                    if not info.get("visible"):
                        continue
                    src = info.get("src") or ""
                    if not src or is_allowed_host_for_session(provider, original_url, src):
                        has_pending_iframe = True
                        break
        if has_password or has_fields:
            return {"reason": "content_ready", "elapsed_ms": int((time.monotonic() - start) * 1000)}
        if has_pending_iframe:
            # A trusted (or not-yet-assigned) iframe destination has not yet
            # produced fillable content -- never declare "dom_stable" out
            # from under it; keep polling (still bounded by `deadline`
            # below) until it resolves or the overall timeout is reached.
            stable_polls = 0
        elif signature is not None and signature == last_signature:
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
    itself trigger a pause.

    Embedded-form-discovery-hardening (2026-08-28): a frame whose OWNING
    `<iframe>` element is currently hidden (e.g. a "switch to application
    form" tab-panel not yet revealed by an apply-entry click) is skipped
    entirely, even when it already contains real fields -- a real, live-
    caught case showed a browser can eagerly load a hidden iframe's content,
    and reporting those fields as discovered here would report
    FORM_ALREADY_VISIBLE and skip the very apply-entry click that reveals
    the form, after which every fill attempt fails outright (`Locator.
    fill()` requires visibility/actionability). Waiting for the apply-entry
    click to reveal it, then re-scanning, is the correct, already-existing
    path -- this only needs to not jump the gun."""
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
            if not frame.frame_element().is_visible():
                continue
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
        result = _wait_for_stable_state(self.page, provider=self.provider, original_url=self.application_url)
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
        outcome = _wait_for_stable_state(self.page, provider=self.provider, original_url=self.application_url)
        route_changed = self.page.url != current_url_before
        if route_changed:
            spa_events.record(spa_events.EVENT_SPA_ROUTE_DETECTED, session_id=self.session_id,
                               provider=self.provider, detail=f"{current_url_before} -> {self.page.url}")
        return {"route_changed": route_changed, **outcome}

    def _do_discover(self) -> DiscoveryOutcome:
        """Thin wrapper over `_do_discover_impl()` (the actual, unmodified
        Phase 10-13 discovery pass -- see its own docstring/body for that
        logic, left entirely untouched by this wrap). Workday/SmartRecruiters/
        Workable browser-assist hardening (2026-08-22): every real discovery
        pass against a real, recognized Workday tenant/site now organically
        contributes ONE `workday_tenant.record_attempt()` row -- the same
        per-attempt evidence Phase 12's validation scripts previously only
        ever produced from bounded, manual runs (docs/workday-observation-
        model.md). Wrapping the whole function (rather than instrumenting
        each of its many early-return pause branches individually) is
        deliberate: it captures EVERY outcome, including the login-gate
        variability Phase 12 found most significant, without touching a
        single line of the existing, already-live-tested discovery logic."""
        outcome = self._do_discover_impl()
        if self.provider == "workday" and self.tenant:
            self._record_workday_attempt(outcome)
        return outcome

    def _record_workday_attempt(self, outcome: DiscoveryOutcome) -> None:
        """Best-effort -- never raises into the discovery pass, matching
        spa_events.record()'s own 'observability must never break the
        caller' contract. `result` mirrors scripts/phase12_live_validation.
        py's own derivation: the apply-entry classification when one was
        found, else the pause reason, else FORM_ALREADY_VISIBLE/NONE."""
        try:
            info = parse_workday_tenant(outcome.current_url or self.application_url)
            control = outcome.apply_entry_control or {}
            result = (
                outcome.pause_reason
                or control.get("classification")
                or ("FORM_ALREADY_VISIBLE" if outcome.fields else "NONE")
            )
            step_indicator = (
                f"{outcome.current_step_observed}/{outcome.total_steps_observed}"
                if outcome.current_step_observed else ""
            )
            workday_tenant.record_attempt(
                self.tenant, self.site, info.host or (urlparse(self.application_url).hostname or ""),
                requisition_id=info.requisition_id, url_initial=self.application_url,
                url_final=outcome.current_url, stage=outcome.stage, apply_control_result=str(result),
                render_time_ms=outcome.render_time_ms, fields_detected=len(outcome.fields),
                resume_upload_detected=any(f.get("type") == "file" for f in outcome.fields),
                step_indicator=step_indicator, result=str(result),
                notes="auto-recorded by browser_runtime._do_discover",
            )
        except Exception:  # noqa: BLE001 -- observability must never break a real session
            pass

    def _do_discover_impl(self) -> DiscoveryOutcome:
        page = self.page
        current_url = page.url
        if not is_allowed_host_for_session(self.provider, self.application_url, current_url):
            return DiscoveryOutcome(pause_reason="PLATFORM_POLICY_RESTRICTED", current_url=current_url)

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
        # Reliable Human-Handoff V1: a real live handoff against Robinhood's
        # Greenhouse posting caught two DISTINCT false-positive sources here,
        # both confirmed by inspecting the actual live DOM (never assumed):
        #
        # 1. Invisible reCAPTCHA (v2 Enterprise "invisible" mode, which this
        #    real posting uses) renders a PERMANENT, non-interactive
        #    "protected by reCAPTCHA" branding badge in the page corner --
        #    class `grecaptcha-badge`/`grecaptcha-logo`/`grecaptcha-error`,
        #    inside an anchor `<iframe title="reCAPTCHA" ... src="...
        #    size=invisible...">`. This badge is present on EVERY page load
        #    (Google's ToS requires it) whether or not a challenge is EVER
        #    shown to anyone -- it is not itself a blocking challenge.
        # 2. The always-present hidden response-token holder (`<textarea
        #    name="g-recaptcha-response">`, h-captcha/turnstile equivalents)
        #    is likewise rendered as soon as the widget library loads,
        #    regardless of solved/challenged state -- reCAPTCHA Enterprise
        #    additionally suffixes its id (observed live:
        #    id="g-recaptcha-response-100000", not the classic
        #    id="g-recaptcha-response"), so matching is done by the stable
        #    `name` attribute, which stays constant across that suffix.
        #
        # Both are excluded from the presence signal itself (never
        # themselves proof of an active block) rather than only via the
        # solved-token check below, since for INVISIBLE reCAPTCHA the token
        # legitimately stays empty for as long as the user is still filling
        # the form -- verification only fires on the bound submit action, so
        # "empty token" cannot distinguish "badge present, nothing has
        # happened yet" from "a real challenge is blocking" in invisible
        # mode. A genuine rendered challenge is a SEPARATE, additional
        # element this exclusion does not touch (a full challenge iframe/
        # modal, e.g. the classic `class="g-recaptcha"` checkbox widget the
        # existing local fixture covers, or a real `bframe` challenge
        # iframe) -- excluding only these specific, well-documented
        # classes/ids can only ever turn an otherwise-true has_captcha into
        # "not blocking" for THOSE elements; it can never mask a
        # differently-classed/id'd genuine challenge element present
        # alongside them.
        _NEVER_BLOCKING_CLASSES = ("grecaptcha-badge", "grecaptcha-logo", "grecaptcha-error")
        _NEVER_BLOCKING_ID_PREFIXES = ("g-recaptcha-response", "h-captcha-response", "cf-turnstile-response")

        def _all_matches_never_blocking(loc) -> bool:
            try:
                count = loc.count()
            except Exception:  # noqa: BLE001
                return False
            if count == 0:
                return False
            for i in range(count):
                el = loc.nth(i)
                try:
                    cls = (el.get_attribute("class") or "")
                    src = (el.get_attribute("src") or "")
                    el_id = (el.get_attribute("id") or "")
                except Exception:  # noqa: BLE001
                    return False
                is_badge_class = any(b in cls for b in _NEVER_BLOCKING_CLASSES)
                is_invisible_anchor_iframe = "size=invisible" in src
                is_response_holder = any(el_id.startswith(p) for p in _NEVER_BLOCKING_ID_PREFIXES)
                if not (is_badge_class or is_invisible_anchor_iframe or is_response_holder):
                    return False
            return True

        iframe_loc = page.locator("iframe[src*='captcha' i]")
        class_loc = page.locator("[class*='captcha' i]")
        id_loc = page.locator("[id*='captcha' i]")
        has_captcha = (
            (iframe_loc.count() > 0 and not _all_matches_never_blocking(iframe_loc))
            or (class_loc.count() > 0 and not _all_matches_never_blocking(class_loc))
            or (id_loc.count() > 0 and not _all_matches_never_blocking(id_loc))
        )
        # A captcha widget's own container element never disappears once
        # rendered -- it stays in the DOM permanently, whether the challenge
        # is still blocking or the human already solved it. Every mainstream
        # CAPTCHA provider (reCAPTCHA, hCaptcha, Cloudflare Turnstile)
        # populates a well-documented, standard hidden response-token field
        # ONLY once solved -- checking that token is non-empty is the
        # correct, narrow precision fix: it can only ever turn an OTHERWISE-
        # true `has_captcha` into "not blocking", never invent a pass when
        # no such token exists, so a genuinely still-unsolved or token-less/
        # custom challenge keeps pausing exactly as before.
        if has_captcha:
            try:
                solved = page.evaluate(
                    """() => {
                        const val = (sel) => (document.querySelector(sel)?.value || '').trim();
                        return val("[name='g-recaptcha-response']") !== ''
                            || val("[name='h-captcha-response']") !== ''
                            || val("[name='cf-turnstile-response']") !== '';
                    }"""
                )
            except Exception:  # noqa: BLE001
                solved = False
            has_captcha = not solved
        if has_captcha:
            provider_health.record_failure(self.provider, provider_health.FailureKind.CAPTCHA,
                                            tenant=self.tenant, site=self.site)
            return DiscoveryOutcome(pause_reason="CAPTCHA_PRESENT", current_url=current_url)

        login_wall = page.locator("input[type=password]").count() > 0
        # Workday + Ashby Provider Execution V1: a real-Chromium OTP fixture
        # caught this live -- a standalone one-time-passcode/2FA challenge
        # screen (no `input[type=password]` at all, just a code field) was
        # previously invisible to this check entirely (it fell through to
        # ordinary field detection), because MFA phrase matching was gated
        # behind `login_wall`. An MFA phrase is specific and narrow enough
        # (see `_MFA_PHRASES`) to stand on its own as a genuine auth-gate
        # signal, independent of whether a password field is also present.
        #
        # Reliable Human-Handoff V1: this must scan VISIBLE text (body_text)
        # rather than raw page.content() -- a real live handoff against
        # Robinhood's Greenhouse posting caught "2fa" matching inside an
        # unrelated Google API proxy iframe's own hashed URL fragment
        # (content.googleapis.com/static/proxy.html, loaded for the page's
        # own "Attach from Google Drive" resume-upload option), nowhere
        # near any actual authentication prompt and never shown to a
        # person. This is the exact same class of bug the Phase 13 CAPTCHA
        # fix already established a fix for (a raw-HTML-source substring
        # scan matching unrelated script/URL text) -- applying the same
        # precision fix here: an MFA phrase actually rendered on the page
        # (a real auth-gate, still caught) reads identically in body_text;
        # only a match confined to markup/script/URL text a person never
        # sees is excluded.
        has_mfa_phrase = any(p in body_text.lower() for p in _MFA_PHRASES)
        if login_wall or has_mfa_phrase:
            provider_health.record_failure(self.provider, provider_health.FailureKind.AUTH_GATE,
                                            tenant=self.tenant, site=self.site)
            if has_mfa_phrase:
                return DiscoveryOutcome(pause_reason="MFA_REQUIRED", current_url=current_url)
            return DiscoveryOutcome(pause_reason="LOGIN_REQUIRED", current_url=current_url)

        start = time.monotonic()
        raw_fields = _detect_fields(page)
        raw_fields = _rescan_unidentifiable_fields(page, raw_fields)
        raw_fields = _rescan_until_field_count_stable(page, raw_fields)
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
            outcome.upload_details.extend(extra.upload_details)
        return outcome

    def _fill_pass(self, raw_fields: list[dict], application_fields: list[ApplicationField]) -> FillOutcome:
        outcome = FillOutcome()
        for rf in raw_fields:
            label = rf.get("label") or rf.get("name") or f"field#{rf.get('index')}"
            field_id, confidence = match_field_with_application_fields(
                rf.get("label", ""), rf.get("name", ""), application_fields,
            )
            app_field = find_field(application_fields, field_id) if field_id else None

            if app_field is not None and app_field.category in SENSITIVE_CATEGORIES and not app_field.auto_fill_allowed:
                decline = _decline_option(rf.get("choices") or [])
                if decline is not None and self._fill_one(rf, decline):
                    outcome.filled.append(label)
                    continue
                if rf.get("required"):
                    outcome.unresolved.append(label)
                continue

            # Browser-Verified Answer Canonical Readiness Integration V1
            # (part 2): a SENSITIVE_CATEGORIES field never gets auto-filled
            # by generic policy/profile matching, full stop -- that rule is
            # unchanged above and below. But a field carrying GENUINE,
            # individually-verified evidence (value_source is set only by
            # record_verified_custom_answer's own live read-back check, the
            # one sanctioned path a human explicitly answers one exact
            # question through) is re-verified against the CURRENT live DOM
            # -- never trusted blindly, since a reconstruction/resume can
            # land on a fresh, unanswered page -- and only THEN treated as
            # resolved. A generic, profile-derived sensitive field
            # (value_source != "browser_verified_field_evidence") never
            # takes this path, regardless of auto_fill_allowed. Without
            # this, a session could never reach READY_FOR_FINAL_SUBMIT at
            # all once any SENSITIVE_CATEGORIES field was on the page, even
            # after a human had explicitly verified every single one.
            if (app_field is not None and app_field.category in SENSITIVE_CATEGORIES
                    and app_field.value_source == "browser_verified_field_evidence" and app_field.auto_fill_allowed):
                actual = self._read_displayed_value(rf)
                expected_norm = str(app_field.verified_value or "").strip().lower()
                actual_norm = (actual or "").strip().lower()
                if expected_norm and actual_norm and (expected_norm in actual_norm or actual_norm in expected_norm):
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
                        outcome.upload_details.append({
                            "canonical_field_id": app_field.field_id,
                            "provider_field_id": rf.get("name") or rf.get("id") or "",
                            "provider_field_label": label,
                            "artifact_path": value,
                        })
                    else:
                        outcome.unresolved.append(label)
                elif rf.get("required"):
                    outcome.unresolved.append(label)
                continue

            value = app_field.verified_value
            choices = rf.get("choices") or []
            # A checkbox's own "choices" is typically just its single
            # self-labeled accessible text, and `_fill_one`'s checkbox
            # branch locates it via `get_by_label(value, exact=False)` --
            # a deliberate LOCATE-BY-SUBSTRING convention (record_verified_
            # custom_answer's own "value" is a short prefix of the full
            # label, never the exact full text). A real live bug: this
            # gate previously required EXACT equality uniformly for every
            # field type, which a self-labeled checkbox's prefix-style
            # verified_value could never satisfy, permanently blocking it
            # from ever auto-resolving even with genuine, durable evidence
            # on file. Radio/select fields keep the stricter exact-match
            # semantics unchanged -- their verified_value must genuinely
            # correspond to ONE enumerated option, never a partial hint.
            value_norm = str(value).strip().lower()
            choices_match = (
                bool(value_norm) and any(
                    value_norm in str(c).strip().lower() or str(c).strip().lower() in value_norm for c in choices
                )
                if rf.get("type") == "checkbox" else
                any(value_norm == str(c).strip().lower() for c in choices)
            )
            if choices and not choices_match:
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
            elif rtype == "combobox":
                return self._fill_combobox(rf, str(value))
            else:
                target.locator(_selector_for(rf)).fill(str(value), timeout=5000)
            return True
        except Exception:  # noqa: BLE001 -- one unfillable field must never abort the whole pass
            return False

    def _discover_owned_listbox(self, target, loc) -> Optional[str]:
        """Reliable Form Interaction V1: resolves the SPECIFIC listbox a
        just-clicked combobox owns -- `aria-controls` first (the standard
        ARIA relationship; react-select-style widgets set this dynamically
        once expanded), else the nearest ancestor wrapper containing a
        `role="listbox"` descendant. NEVER a document-wide `[role=option]`
        search -- a real live bug: an unrelated, already-in-DOM-but-hidden
        widget (a phone number field's own international-dialing-code
        picker, which pre-renders ~240 country options off-screen) was
        picked up by a document-wide option scan and silently selected
        instead of the actually-clicked combobox's real option ("United
        States +1" instead of "United States" for the application's
        address Country field).

        `aria-controls` is read with a short bounded retry, not a single
        synchronous read right after `.click()` -- a SECOND real live
        occurrence of the exact same wrong-widget selection was traced to
        this: React sets `aria-controls` asynchronously on expand, so a
        read immediately after click can still see it empty and fall
        through to the (less precise) ancestor search, which climbed far
        enough to find a DIFFERENT nearby field's listbox instead. Giving
        the attribute a brief chance to appear fixes the race at its
        actual source rather than trying to make the fallback smarter."""
        # Reliable Form Interaction V1: a real sequential live run (15
        # fields filled in one pass against the real Robinhood/Greenhouse
        # form) caught this retry budget being too short under real load --
        # a field late in the sequence failed here with an EXACT real
        # option available (confirmed by direct comparison against the
        # identical logic run in isolation, which succeeded reliably),
        # while an earlier, less-loaded field succeeded. 10x150ms is the
        # budget proven reliable in that direct comparison; 5x100ms was not.
        aria_controls = None
        for _ in range(10):
            try:
                aria_controls = loc.get_attribute("aria-controls")
            except Exception:  # noqa: BLE001
                aria_controls = None
            if aria_controls:
                break
            target.wait_for_timeout(150)
        if aria_controls:
            return f"[id='{_css_attr_escape(aria_controls.split()[0])}']"
        try:
            handle = loc.element_handle()
            if handle is None:
                return None
            wrapper_id = target.evaluate(
                """(el) => {
                    let node = el;
                    for (let i = 0; i < 5 && node; i++) {
                        node = node.parentElement;
                        if (node && node.querySelector('[role="listbox"]')) {
                            if (!node.id) node.id = 'sja-wrap-' + Math.random().toString(36).slice(2);
                            return node.id;
                        }
                    }
                    return null;
                }""",
                handle,
            )
        except Exception:  # noqa: BLE001
            wrapper_id = None
        if wrapper_id:
            return f"[id='{_css_attr_escape(wrapper_id)}'] [role='listbox']"
        return None

    def _close_popup(self, loc) -> None:
        try:
            loc.press("Escape", timeout=2000)
        except Exception:  # noqa: BLE001
            pass

    def _clear_typed_value(self, loc) -> None:
        """Best-effort: resets a combobox's typed-but-unmatched filter text
        back to blank on a failed selection, so the field honestly shows
        "not yet answered" rather than a value that looks like a real
        (wrong) answer someone might mistake for intentional."""
        try:
            loc.fill("", timeout=2000)
        except Exception:  # noqa: BLE001
            pass

    def _read_display_flag_class(self, rf: dict) -> Optional[str]:
        """Provider-Semantic Selection Verification V1: reads the CURRENT
        display's flag-icon class, scoped to a genuine `single-value`
        display element first -- never an independent ancestor climb on
        its own, which can match a DIFFERENT flag icon belonging to the
        (still-in-DOM, merely hidden) listbox's OWN option elements sitting
        in the same ancestor subtree as the real display. Returns None
        when no single-value element, or no flag icon within it, is found
        -- never guessed. Shared by `_fill_combobox`'s own post-selection
        check and `_verify_combobox_value`'s standalone re-verification."""
        target = rf.get("_frame") or self.page
        try:
            return target.evaluate(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return null;
                    let node = el;
                    for (let i = 0; i < 6 && node; i++) {
                        node = node.parentElement;
                        if (!node) break;
                        const single = node.querySelector('[class*="single-value" i]');
                        if (single) {
                            const flag = single.querySelector('[class*="flag" i]');
                            return flag ? flag.className : null;
                        }
                    }
                    return null;
                }""",
                _selector_for(rf),
            )
        except Exception:  # noqa: BLE001
            return None

    def _verify_combobox_value(self, rf: dict, expected_value: str) -> Optional[bool]:
        """Provider-Semantic Selection Verification V1: a READ-ONLY re-
        check of the field's CURRENT state against `expected_value` --
        never re-selects anything, never changes the field's current
        answer. Exists for `app.applications.browser_assist.record_
        verified_custom_answer()`'s own independent post-fill sanity check
        (guarding against a DIFFERENT nearby field having been filled by
        mistake), which previously only ever compared DISPLAYED TEXT -- a
        real gap for a field whose display is a non-identifying fragment
        shared by multiple options (a dial code like "+1"), where
        `_fill_combobox` itself had already genuinely verified the correct
        option via the flag-icon self-consistency check, only for this
        SEPARATE, text-only re-check to then reject it as a false failure.

        Tries text first (cheap, no DOM interaction); if inconclusive,
        opens the dropdown to find `expected_value`'s own option and read
        ITS flag class (this is a lookup, never a selection -- the popup is
        always closed via Escape afterward, never by clicking an option),
        then compares it to the currently-displayed flag. Returns None
        (never a guessed True/False) when neither signal is available at
        all -- the caller falls back to its own existing behavior."""
        target = rf.get("_frame") or self.page
        displayed = self._read_displayed_value(rf)
        desired_norm = expected_value.strip().lower()
        if displayed:
            t = displayed.strip().lower()
            if desired_norm in t or t in desired_norm:
                return True
        try:
            loc = target.locator(_selector_for(rf))
            loc.click(timeout=3000)
            listbox_sel = self._discover_owned_listbox(target, loc)
            if listbox_sel is None:
                self._close_popup(loc)
                return None
            try:
                loc.fill(expected_value, timeout=2000)
            except Exception:  # noqa: BLE001
                pass
            options = target.locator(f"{listbox_sel} [role='option']")
            count = options.count()
            expected_flag_class = None
            for i in range(count):
                text = (options.nth(i).inner_text() or "").strip().lower()
                if desired_norm in text or text in desired_norm:
                    try:
                        expected_flag_class = options.nth(i).locator('[class*="flag" i]').first.get_attribute(
                            "class", timeout=500,
                        )
                    except Exception:  # noqa: BLE001
                        expected_flag_class = None
                    break
            self._close_popup(loc)
            self._clear_typed_value(loc)
            if not expected_flag_class:
                return None
            observed_flag_class = self._read_display_flag_class(rf)
            if observed_flag_class:
                return observed_flag_class == expected_flag_class
            return None
        except Exception:  # noqa: BLE001
            return None

    def _fill_combobox(self, rf: dict, value: str) -> bool:
        """Reliable Form Interaction V1: click-open -> type-filter ->
        SCOPED option match (within the field's own owned listbox only,
        see `_discover_owned_listbox`) -> click -> verify the field's own
        displayed value actually reflects the selection -> close the
        popup. Never claims success from a mere click/fill not throwing --
        the post-selection displayed value is the only proof accepted
        (CLAUDE.md Reliable Form Interaction V1 Phase 3/14: "the final
        displayed/selected state is the proof")."""
        target = rf.get("_frame") or self.page
        try:
            loc = target.locator(_selector_for(rf))
            loc.click(timeout=5000)
            listbox_sel = self._discover_owned_listbox(target, loc)
            if listbox_sel is None:
                self._close_popup(loc)
                return False
            try:
                loc.fill(value, timeout=3000)
            except Exception:  # noqa: BLE001
                pass  # some comboboxes are click-only, not searchable -- fine
            options = target.locator(f"{listbox_sel} [role='option']")
            count = 0
            for _ in range(3):
                target.wait_for_timeout(250)
                count = options.count()
                if count:
                    break
            if count == 0:
                self._close_popup(loc)
                self._clear_typed_value(loc)
                return False

            desired_norm = value.strip().lower()
            match_idx = None
            option_texts = []
            for i in range(count):
                text = (options.nth(i).inner_text() or "").strip()
                option_texts.append(text)
                if text.lower() == desired_norm:
                    match_idx = i
                    break
            if match_idx is None:
                for i, text in enumerate(option_texts):
                    tl = text.lower()
                    if desired_norm in tl or tl in desired_norm:
                        match_idx = i
                        break
            if match_idx is None:
                # No confident match among the field's OWN rendered options
                # -- never guess. Also never leave the unmatched typed text
                # sitting in the field looking like a real (wrong) answer.
                self._close_popup(loc)
                self._clear_typed_value(loc)
                return False

            chosen_text = option_texts[match_idx].strip().lower()
            # Provider-Semantic Selection Verification V1: some fields (a
            # real, live-observed phone-country-code picker) display only a
            # non-identifying fragment after selection (a dial code shared
            # by multiple countries, e.g. "+1" for the US/Canada/Puerto
            # Rico/...) -- text-based verification below can never honestly
            # confirm WHICH option that represents, and correctly refuses
            # to guess from the dial code alone. This captures the CLICKED
            # option's own flag-icon class (a stable, provider-supplied
            # semantic marker widely used by phone-input libraries --
            # `[class*="flag" i]` matches the common naming convention
            # broadly, not any one specific library or employer) BEFORE the
            # click, so it can be compared against the post-selection
            # display's OWN flag class as a structural self-consistency
            # check: not "does this mean the United States" (never
            # inferred), only "is the option now displayed the exact same
            # one that was clicked" -- genuine verification, not a guess.
            try:
                expected_flag_class = options.nth(match_idx).locator('[class*="flag" i]').first.get_attribute(
                    "class", timeout=1000,
                )
            except Exception:  # noqa: BLE001 -- no flag icon on this field's options; contributes nothing below
                expected_flag_class = None
            options.nth(match_idx).click(timeout=5000)
            target.wait_for_timeout(200)

            # Reliable Form Interaction V1: react-select-style widgets clear
            # the search input's OWN value back to "" once a real selection
            # is made -- the chosen value instead renders in a SEPARATE
            # sibling "single value" display element. A real live bug: the
            # old verification read only input_value() and compared it with
            # a bidirectional substring check ("x in y or y in x"); Python's
            # `"" in y` is always True, so an empty (correctly-cleared)
            # input silently PASSED verification no matter what was
            # actually selected -- this masked the exact wrong-option
            # selection bug this module exists to catch. The display
            # element (matched by the stable `single-value` class fragment,
            # not the CSS-module-hashed suffix) is checked FIRST and is
            # authoritative when present; input_value() is only the
            # fallback for a plain text/autocomplete field that genuinely
            # has no such display element (e.g. Location/City).
            #
            # A genuinely multi-select react-select field (a real Figma
            # posting's "primary technical expertise"/"programming
            # languages" questions, both rendered with a `[]`-suffixed
            # field id) renders each chosen option as a `multi-value` chip
            # instead of a `single-value` element -- a real live gap: with
            # no multi-value fallback, this check found nothing, fell
            # through to input_value() (always "" right after a react-select
            # selection, same as single-select), and reported False even
            # though the click+select genuinely succeeded, permanently
            # blocking these fields from ever verifying. Checked only when
            # no single-value element is found at that same ancestor level.
            try:
                displayed = target.evaluate(
                    """(sel) => {
                        const el = document.querySelector(sel);
                        if (!el) return null;
                        let node = el;
                        for (let i = 0; i < 6 && node; i++) {
                            node = node.parentElement;
                            if (!node) break;
                            const single = node.querySelector('[class*="single-value" i]');
                            if (single) return single.innerText;
                            const labels = node.querySelectorAll('[class*="multi-value__label" i]');
                            if (labels.length) return Array.from(labels).map(m => m.innerText).join(', ');
                            const multi = node.querySelectorAll('[class*="multi-value" i]');
                            if (multi.length) return Array.from(multi).map(m => m.innerText).join(', ');
                        }
                        return null;
                    }""",
                    _selector_for(rf),
                )
            except Exception:  # noqa: BLE001
                displayed = None
            try:
                current = loc.input_value(timeout=2000)
            except Exception:  # noqa: BLE001
                current = None
            observed_flag_class = self._read_display_flag_class(rf) if expected_flag_class else None
            try:
                expanded = loc.get_attribute("aria-expanded", timeout=2000) or ""
            except Exception:  # noqa: BLE001
                expanded = ""
            self._close_popup(loc)
            if expanded == "true":
                return False  # popup never closed -- do not claim success

            # Provider-Semantic Selection Verification V1: checked BEFORE
            # the text-based candidates below, not after. A field whose
            # display text is a non-identifying fragment shared by several
            # real options (a dial code like "+1") makes the ordinary text
            # comparison return a CONFIDENT False (the text genuinely
            # doesn't match the country name) rather than "inconclusive" --
            # trying text first would short-circuit and never reach this
            # more specific, structural signal at all. A field with no
            # flag icon (expected_flag_class is None) never enters this
            # branch, so ordinary fields are completely unaffected.
            if expected_flag_class and observed_flag_class and observed_flag_class == expected_flag_class:
                return True

            def _matches(text: Optional[str]) -> Optional[bool]:
                if not text:
                    return None  # inconclusive -- caller tries the next source
                t = text.strip().lower()
                return t == chosen_text or desired_norm in t or t in desired_norm

            for candidate in (displayed, current):
                verdict = _matches(candidate)
                if verdict is not None:
                    return verdict
            # No source yielded any conclusive, non-guessed evidence --
            # never claim success on silence.
            return False
        except Exception:  # noqa: BLE001
            return False

    def _read_displayed_value(self, rf: dict) -> Optional[str]:
        """Browser-Verified Answer Canonical Readiness Integration V1: reads
        a field's CURRENT real displayed/selected value -- the same
        "display element first, input_value() fallback" logic
        `_fill_combobox` uses for its own post-selection verification,
        exposed standalone so a caller can re-check an ALREADY-filled
        field's live state (e.g. app.applications.browser_assist.
        record_verified_custom_answer(), immediately after a fill, to
        capture the exact evidence text durably). Never mutates anything.

        The ancestor-climbing "single-value" display search is only ever
        attempted for `type == "combobox"` fields -- a real live bug: for
        an ordinary text field, that search could climb past the field's
        own (nonexistent) display wrapper and find a DIFFERENT, nearby
        combobox field's single-value element instead, since real forms
        commonly place several fields close together in the DOM. A plain
        text/tel/textarea field has no such display element of its own by
        construction, so it goes straight to input_value(); a checkbox
        goes straight to is_checked().

        A genuinely multi-select react-select field renders each chosen
        option as a `multi-value` chip instead of a `single-value` element
        -- checked as a fallback at the same ancestor level, same rationale
        as `_fill_combobox`'s own identical fallback (a real live gap found
        against a real Figma posting's multi-select questions)."""
        target = rf.get("_frame") or self.page
        try:
            loc = target.locator(_selector_for(rf))
        except Exception:  # noqa: BLE001
            return None
        displayed = None
        if rf.get("type") == "combobox":
            try:
                displayed = target.evaluate(
                    """(sel) => {
                        const el = document.querySelector(sel);
                        if (!el) return null;
                        let node = el;
                        for (let i = 0; i < 6 && node; i++) {
                            node = node.parentElement;
                            if (!node) break;
                            const single = node.querySelector('[class*="single-value" i]');
                            if (single) return single.innerText;
                            const labels = node.querySelectorAll('[class*="multi-value__label" i]');
                            if (labels.length) return Array.from(labels).map(m => m.innerText).join(', ');
                            const multi = node.querySelectorAll('[class*="multi-value" i]');
                            if (multi.length) return Array.from(multi).map(m => m.innerText).join(', ');
                        }
                        return null;
                    }""",
                    _selector_for(rf),
                )
            except Exception:  # noqa: BLE001
                displayed = None
        if displayed:
            return displayed
        try:
            if rf.get("type") == "checkbox":
                return "true" if loc.is_checked(timeout=2000) else "false"
            return loc.input_value(timeout=2000)
        except Exception:  # noqa: BLE001
            return None

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
            fields_before = _fingerprint_fields(_detect_fields(self.page))
        except Exception:  # noqa: BLE001 -- a pre-click scan failure must never block the click itself
            fields_before = None
        try:
            if next_button.get("id"):
                self.page.locator(f"#{next_button['id']}").click(timeout=5000)
            else:
                self.page.get_by_text(next_button["text"], exact=False).first.click(timeout=5000)
        except Exception:  # noqa: BLE001 -- a failed click still means "did not advance"
            return {"advanced": False, "reason": "click on next/continue control failed"}
        route = self._do_advance_to_route(before_url)
        # Workday/SmartRecruiters/Workable browser-assist hardening
        # (2026-08-22): a click that changed neither the route NOR the
        # field-set fingerprint may mean the form is genuinely stuck behind
        # inline client-side validation (a required field left empty) --
        # see app.applications.dynamic_validation. Strictly additive: any
        # route OR field-set change still falls straight through to the
        # unchanged "advanced: True" behavior below; this only intervenes
        # for the previously-unhandled "nothing changed" case, and even
        # then only when real DOM/text validation-error evidence is found
        # (NO_CHANGE_UNKNOWN -- nothing changed, no evidence either way --
        # is never guessed as a block; it also falls through unchanged).
        if not route["route_changed"] and fields_before is not None:
            try:
                fields_after = _fingerprint_fields(_detect_fields(self.page))
            except Exception:  # noqa: BLE001
                fields_after = fields_before
            if fields_after == fields_before:
                validation = _detect_validation_errors(self.page)
                attempt = AdvanceAttempt(
                    route_changed=False, fields_changed=False,
                    validation_error_elements=validation["count"], body_text=validation["body_text"],
                )
                if classify_advance_attempt(attempt) == AdvanceOutcome.VALIDATION_BLOCKED:
                    spa_events.record(spa_events.EVENT_VALIDATION_BLOCKED, session_id=self.session_id,
                                       provider=self.provider, detail="; ".join(validation["errors"][:5]))
                    return {"advanced": False, "reason": "validation_blocked",
                            "validation_errors": validation["errors"]}
        self.current_step += 1
        return {"advanced": True, "current_step": self.current_step}

    def _do_capture_confirmation(self) -> ConfirmationOutcome:
        current_url = self.page.url
        try:
            text = self.page.inner_text("body")
        except Exception:  # noqa: BLE001
            text = ""
        # Real Provider Execution V1: the text-side observation now comes
        # from the shared, pure `app.applications.confirmation_parser`
        # (identical tables and identical ordering -- duplicate-application
        # evidence is still checked FIRST and returned distinctly, per
        # CLAUDE.md Phase 11 section 36; a real success PHRASE match is still
        # required, per section 35). Only the ownership of the tables moved.
        parsed = parse_confirmation_text(text)
        if parsed.already_applied:
            return ConfirmationOutcome(confirmed=False, current_url=current_url, already_applied=True)
        phrase_matched = parsed.phrase_matched
        confirmation_id = parsed.confirmation_id
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
        return ConfirmationOutcome(
            confirmed=True, current_url=current_url, confirmation_id=confirmation_id,
            confirmation_text_fingerprint=parsed.text_fingerprint, evidence_strength=grade.strength.value,
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


def _css_attr_escape(value: str) -> str:
    """Escapes a value for use inside a CSS attribute-selector string
    literal (`[attr='...']`). Only quotes and backslashes need escaping
    there -- unlike an `#id` selector, an attribute selector never requires
    special handling for a value that starts with a digit."""
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def _selector_for(rf: dict) -> str:
    """Reliable Form Interaction V1: an `id`/`name` is always resolved via
    an ATTRIBUTE selector (`[id='...']`/`[name='...']`), never `#id` --
    `#1255` is invalid CSS (an ID selector can't start with a digit without
    manual escaping), a real bug this fixes (a live Greenhouse "gender
    identity" field with `id="1255"` threw a Playwright SyntaxError on
    every fill attempt). When neither is present, the field's own
    `data-sja-idx` DOM marker (stamped by `_detect_fields()` on the actual
    element, not a document position) is used -- never the old
    `:nth-match(...)` positional fallback, which silently re-targets the
    WRONG field once an earlier interaction (e.g. opening one combobox's
    listbox) inserts/removes sibling nodes elsewhere on the page."""
    if rf.get("id"):
        return f"[id='{_css_attr_escape(rf['id'])}']"
    if rf.get("name"):
        return f"[name='{_css_attr_escape(rf['name'])}']"
    if rf.get("sja_idx") is not None:
        return f"[data-sja-idx='{_css_attr_escape(rf['sja_idx'])}']"
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


def _is_unidentifiable(rf: dict) -> bool:
    """True for a field with no label, no id, and no name -- nothing a
    filler or a human reviewer could ever address it by. See
    `_rescan_unidentifiable_fields`'s docstring for why this specific shape
    gets one bounded rescan rather than being surfaced immediately."""
    return not rf.get("label") and not rf.get("id") and not rf.get("name")


def _rescan_unidentifiable_fields(page, raw_fields: list[dict]) -> list[dict]:
    """One bounded rescan when the initial `_detect_fields()` pass caught a
    widget mid-hydration (see `config.BROWSER_FIELD_RESCAN_WAIT_MS`'s
    docstring for the real, live-observed case this fixes). Only ever
    REPLACES the result with a strictly better one (fewer unidentifiable
    fields than the previous attempt) -- if a rescan doesn't improve things,
    the original (or best-so-far) scan is kept and returned as-is, so this
    can never fabricate a field's identity or silently drop a genuinely
    unidentifiable field from the report. Bounded by
    `BROWSER_FIELD_RESCAN_MAX_ATTEMPTS`, never an unbounded retry loop."""
    best = raw_fields
    best_unidentifiable = sum(1 for rf in best if _is_unidentifiable(rf))
    if best_unidentifiable == 0:
        return best
    for _ in range(max(0, config.BROWSER_FIELD_RESCAN_MAX_ATTEMPTS)):
        try:
            page.wait_for_timeout(max(0, config.BROWSER_FIELD_RESCAN_WAIT_MS))
            rescanned = _detect_fields(page)
        except Exception:  # noqa: BLE001 -- a page mid-navigation may throw transiently; keep the best-so-far
            break
        unidentifiable = sum(1 for rf in rescanned if _is_unidentifiable(rf))
        if unidentifiable < best_unidentifiable:
            best, best_unidentifiable = rescanned, unidentifiable
        if best_unidentifiable == 0:
            break
    return best


def _rescan_until_field_count_stable(page, raw_fields: list[dict]) -> list[dict]:
    """Form-Fingerprint Stability V1: a bounded rescan for the DIFFERENT
    shape `_rescan_unidentifiable_fields` doesn't cover -- not a single
    field caught mid-hydration, but a WHOLE ADDITIONAL SECTION (a real,
    live-observed case: a conditionally-rendered demographic/EEO block,
    including its own companion consent checkbox, that some real ATS
    postings load asynchronously and only mount a short time after the
    rest of the form is already interactive) still appearing after the
    initial `_detect_fields()` pass returns.

    This is the actual root cause behind a real, live-observed instability:
    two genuinely identical page loads of the same posting could each
    capture a DIFFERENT total field count (whichever fields had mounted by
    the moment `_detect_fields()` happened to run), which then correctly
    -- `_fingerprint_fields()` itself was never the bug -- produces a
    DIFFERENT fingerprint for what is semantically the identical form,
    manifesting as spurious PAUSED_FORM_CHANGED pauses and, separately, a
    field that appears "missing" on some discovery passes and not others.
    The fix belongs here, upstream of fingerprinting, not in
    `_fingerprint_fields()`'s own hashing logic, which already correctly
    fingerprints semantic form state (name/label/type/required/choices)
    and must keep genuinely invalidating on a real material change.

    Only ever replaces the result with a result that has MORE fields than
    the current best -- this can never lose a field a prior scan already
    found, and stops as soon as growth stops (a field count that came back
    the same as the last scan is treated as settled, not re-tried for the
    remaining budget) -- bounded by the same
    `BROWSER_FIELD_RESCAN_MAX_ATTEMPTS`/`_WAIT_MS` budget
    `_rescan_unidentifiable_fields` already uses for this same general
    class of concern (give an async-hydrating form a little more time),
    never a separate or unbounded wait."""
    best = raw_fields
    for _ in range(max(0, config.BROWSER_FIELD_RESCAN_MAX_ATTEMPTS)):
        try:
            page.wait_for_timeout(max(0, config.BROWSER_FIELD_RESCAN_WAIT_MS))
            rescanned = _detect_fields(page)
        except Exception:  # noqa: BLE001 -- a page mid-navigation may throw transiently; keep the best-so-far
            break
        if len(rescanned) > len(best):
            best = rescanned
        else:
            break  # no further growth observed -- settled, stop early
    return best


def _detect_fields(page) -> list[dict]:
    """Real-browser DOM scan: input/textarea/select, plus grouped radio/
    checkbox sets by `name`, with label/aria-label/placeholder/fieldset-legend
    fallback and a required-indicator check (CLAUDE.md Phase 10 section 9).
    Pierces OPEN shadow roots (CLAUDE.md Phase 12 sections 13, 15, 60-62) via
    the shared `_DEEP_QUERY_JS` helper -- a closed shadow root's contents
    simply aren't found, the correct honest outcome rather than a bypass
    attempt. Never touches password/hidden/submit/button inputs -- those are
    always left for the human (password) or handled separately (submit).
    Also never counts a `<header>`/`<nav>`/`role="search|banner|navigation"`
    landmark's fields, or any `type="search"` input, as application content
    -- see `_CHROME_LANDMARK_SELECTOR`'s docstring for the real defect this
    closes (a page's own unrelated header search box being mistaken for the
    application form itself).
    `page` may also be a Playwright Frame -- both expose `.evaluate()`
    identically, so this same function scans an allowed-host iframe's
    document too (CLAUDE.md Phase 12 section 14).

    Reliable Form Interaction V1: every detected element is also stamped
    with a `data-sja-idx` attribute (a live DOM marker, not a document
    position) and `sja_idx` is returned in the raw field dict --
    `_selector_for()` prefers this over the old positional `:nth-match(...)`
    fallback when a field has neither `id` nor `name`, since a marker tied
    to the actual element survives unrelated DOM churn elsewhere on the
    page (a real bug: `:nth-match` silently re-targeted a DIFFERENT field
    after opening one combobox inserted/removed sibling nodes). An
    `<input role="combobox">` (the react-select-style pattern used for
    every Greenhouse custom question, Country, and Location) is classified
    `type: 'combobox'` rather than its raw `text`/`tel`/etc DOM type, so
    `_fill_one()` can dispatch it to the listbox-aware `_fill_combobox()`
    instead of a blind `.fill()`. A field's label also falls back to a
    `role="group"` ancestor's `aria-labelledby` target text (the pattern
    Greenhouse uses for its file-upload widgets, whose OWN `<label>` is a
    generic, visually-hidden "Attach" -- the real "Resume/CV"/"Cover
    Letter" heading is a sibling `<div>` referenced only via
    `aria-labelledby` on the enclosing `role="group"`)."""
    return page.evaluate(
        """
        (chromeSelector) => {"""
        + _DEEP_QUERY_JS +
        """
          const results = [];
          const seenGroups = new Set();
          __deepQueryAll(document, 'input, textarea, select').forEach((el, idx) => {
            const type = (el.getAttribute('type') || el.tagName).toLowerCase();
            if (['hidden', 'password', 'submit', 'button', 'image', 'search'].includes(type)) return;
            if (el.closest(chromeSelector)) return;
            // A real, live-observed react-select pattern (Anthropic's newest
            // Greenhouse UI): alongside its genuine, properly-labeled
            // role="combobox" control, react-select also renders a
            // PERMANENT, aria-hidden="true", tabindex="-1" dummy
            // <input required> whose only purpose is letting native HTML5
            // form validation fire (react-select's own "RequiredInput"
            // component) -- it is explicitly hidden from assistive tech,
            // never holds or submits the real selected value, and was never
            // meant to be perceived as its own question. Confirmed via a
            // real live DOM capture, not inferred: this element's own
            // outerHTML is exactly
            // `<input required tabindex="-1" aria-hidden="true" ...>` with
            // no label/id/name, sitting as a sibling of the real combobox.
            // Skipping any aria-hidden element (or one inside an
            // aria-hidden ancestor) is a general, non-Anthropic-specific
            // rule: an element explicitly hidden from assistive technology
            // was never meant to be surfaced as something requiring a
            // person's input, matching this scan's existing "only
            // user-facing content" philosophy for hidden/password/submit/
            // landmark elements above.
            if (el.getAttribute('aria-hidden') === 'true' || el.closest('[aria-hidden="true"]')) return;

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
            let groupLabel = '';
            const grp = el.closest('[role="group"]');
            if (grp) {
              const labelledby = grp.getAttribute('aria-labelledby');
              if (labelledby) {
                const texts = labelledby.split(/\\s+/).map((id) => {
                  const ref = document.getElementById(id);
                  return ref ? ref.innerText.trim() : '';
                }).filter(Boolean);
                groupLabel = texts.join(' ');
              }
            }
            const required = !!(el.required || el.getAttribute('aria-required') === 'true');
            el.setAttribute('data-sja-idx', String(idx));

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
                index: idx, label: (fieldsetLabel || label || groupLabel || '').trim(), type: type,
                name: el.name || '', id: '', required: required, choices: choices, sja_idx: idx,
              });
              return;
            }

            let choices = [];
            if (el.tagName === 'SELECT') {
              choices = Array.from(el.options).map((o) => o.textContent.trim()).filter(Boolean);
            }
            const resolvedType = (el.getAttribute('role') === 'combobox') ? 'combobox' : type;
            // A FILE input's own <label> commonly captions the button
            // action ("Attach"), identically for every upload field on the
            // page -- never the actual field identity. For file inputs
            // ONLY, a role="group" ancestor's aria-labelledby (which
            // Greenhouse's real Resume/CV vs Cover Letter widgets both
            // carry, pointing at a sibling "Resume/CV"/"Cover Letter"
            // heading) wins over that generic per-button label. Every
            // other field type keeps its unchanged, existing priority.
            const resolvedLabel = (type === 'file' && groupLabel)
              ? groupLabel : (label || fieldsetLabel || groupLabel || '');
            results.push({
              index: idx, label: resolvedLabel.trim(), type: resolvedType,
              name: el.name || '', id: el.id || '', required: required, choices: choices, sja_idx: idx,
            });
          });
          return results;
        }
        """,
        _CHROME_LANDMARK_SELECTOR,
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


_VALIDATION_ERROR_SELECTOR = (
    "[aria-invalid='true'], [role='alert'], .error, .field-error, .invalid-feedback, "
    ".validation-error, .validation-message"
)


def _detect_validation_errors(page) -> dict:
    """Workday/SmartRecruiters/Workable browser-assist hardening
    (2026-08-22): scans for visible validation-error-shaped elements (an
    aria-invalid field, a role=alert region, or a commonly-classed error
    message), pierced through open shadow roots via the shared
    _DEEP_QUERY_JS helper like every other DOM scan in this module. Only
    ever consulted by _do_advance_step() when a Next/Continue click
    produced NO route change and NO field-set change -- see
    app.applications.dynamic_validation."""
    try:
        texts = page.evaluate(
            """
            (selector) => {"""
            + _DEEP_QUERY_JS +
            """
              const isVisible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
              const els = __deepQueryAll(document, selector);
              return els.filter(isVisible).map((el) => (el.innerText || '').trim()).filter(Boolean).slice(0, 10);
            }
            """,
            _VALIDATION_ERROR_SELECTOR,
        )
    except Exception:  # noqa: BLE001 -- a malformed page must never break advance_step
        texts = []
    try:
        body_text = page.inner_text("body")
    except Exception:  # noqa: BLE001
        body_text = ""
    return {"count": len(texts), "errors": texts, "body_text": body_text}


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


def fill_one_field(session_id: str, rf: dict, value: str) -> bool:
    """Browser-Verified Answer Canonical Readiness Integration V1: public
    single-field fill, dispatching through the SAME `_fill_one` type-aware
    logic (including `_fill_combobox`'s scoped-listbox resolution and
    displayed-value verification) every other fill path in this project
    uses. Used by app.applications.browser_assist.
    record_verified_custom_answer() to apply one explicit, human-provided
    answer to one specific field -- never a batch/positional operation."""
    live = _get_live(session_id)
    return live.run(live._fill_one, rf, value, timeout=15)


def read_displayed_value(session_id: str, rf: dict) -> Optional[str]:
    """Public wrapper for `_LiveSession._read_displayed_value` -- reads a
    field's CURRENT real displayed/selected value without filling
    anything. See that method's docstring."""
    live = _get_live(session_id)
    return live.run(live._read_displayed_value, rf, timeout=10)


def verify_combobox_value(session_id: str, rf: dict, expected_value: str) -> Optional[bool]:
    """Public wrapper for `_LiveSession._verify_combobox_value` -- a READ-
    ONLY re-check of a combobox field's current state against
    `expected_value`, trying text first and a flag-icon structural
    self-consistency check second. Never re-selects anything. Used by
    app.applications.browser_assist.record_verified_custom_answer() as a
    fallback when its own bidirectional-substring text check is
    inconclusive for a field whose display is a non-identifying fragment
    (see that method's docstring)."""
    live = _get_live(session_id)
    return live.run(live._verify_combobox_value, rf, expected_value, timeout=15)


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
    """Used only when a session must be dropped WITHOUT a normal
    close_session() call -- currently open_session()'s except-branch, when
    `_do_open` itself raised (e.g. a navigation timeout after
    `browser.launch()` already succeeded). Must still submit `_do_close` to
    the session's own dedicated thread (never call it from this thread --
    Playwright's sync API is not thread-safe) so the already-launched
    Chromium/Playwright driver process is actually torn down rather than
    orphaned. `wait=False` below only means this calling thread doesn't
    block on it -- the submitted close still runs to completion on the
    session's own worker thread once whatever `_do_open` was doing finishes
    or raises."""
    with _REGISTRY_LOCK:
        live = _REGISTRY.pop(session_id, None)
    if live is not None:
        try:
            live.executor.submit(live._do_close)
        except Exception:  # noqa: BLE001 -- best-effort cleanup, never raises
            pass
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
