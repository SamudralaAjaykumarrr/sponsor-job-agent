# Application Browser Assist

> **Phase 10 update:** this document describes the original Phase 9
> `prepare_application()` one-shot helper, which still exists unchanged and still works exactly
> as documented below. For the production-quality, resumable, session-based system built on top
> of it in Phase 10 (persistent visible window, multi-step forms, pause/resume, crash recovery,
> post-manual-submit confirmation capture), see **`docs/browser-assist-sessions.md`** and
> **`docs/phase10-real-ats-assist.md`**.

## What it is

`app/applications/browser_assist.py::prepare_application()` is an OPTIONAL, off-by-default
(`BROWSER_ASSIST_ENABLED=false`) visible-browser preparation aid, built on Playwright. It opens
a job's real application URL, detects form fields via a DOM scan, fills only verified,
non-sensitive candidate values (reusing the exact same deterministic
`app.applications.mapping.match_field()` engine every `ApplicationProvider` adapter already
uses — never a second, different matching heuristic), prepares a resume/cover-letter file
upload, and returns a `HandoffRecord` describing exactly what's left for the human.

## What it will never do

- No stealth plugins, no browser-fingerprint spoofing, no CAPTCHA solving, no proxy rotation,
  no anti-bot bypass, no hidden/automated login, no MFA interception.
- **Never clicks a final submit/apply action**, under any condition. There is no code path in
  this module that performs an irreversible action.
- Never fills a `DEMOGRAPHICS`/`VOLUNTARY_DISCLOSURE`/`LEGAL_ATTESTATION`/`SIGNATURE`-category
  field, even when a verified profile value exists — those always land in
  `unresolved_field_ids` for the human.
- Never auto-fills a field the mapping engine only matched at `LOW` confidence.
- Stops immediately (`stage="USER_ACTION_REQUIRED"`) on any of: a CAPTCHA indicator in the page
  (a `captcha` string match or a CAPTCHA-hosting iframe), or a password input anywhere on the
  page (treated as a login/account wall).

## Session safety (CLAUDE.md section 22)

Every call opens a **fresh, ephemeral** Playwright browser context
(`browser.new_context()`) — never `launch_persistent_context()` with a reused profile
directory, never a saved `storage_state`. The context (and its cookies, local storage, and any
in-memory session) is destroyed (`context.close()`) at the end of every call, success or
failure. No password, MFA code, long-lived cookie, or auth token is ever written to disk.
`BROWSER_ASSIST_PROFILE_DIR` (`data/browser_assist_runtime/`, gitignored) exists only as a
scratch directory for any transient download artifact Playwright might produce — it is never
used to persist a browser profile or session state.

## Installing (optional)

```
pip install -r requirements-dev.txt      # includes playwright>=1.40
playwright install chromium               # downloads the browser binary
# Linux only, if system shared libraries are missing (requires root):
playwright install-deps chromium
```

`app.applications.browser_assist.playwright_available()` returns `False` (and
`prepare_application()` raises `BrowserAssistUnavailable` rather than silently no-op-ing) if
either the package or `BROWSER_ASSIST_ENABLED` isn't set — this feature is never silently
"on".

## The `HandoffRecord` (CLAUDE.md section 23)

```python
@dataclass
class HandoffRecord:
    job_id: int
    company: str
    application_url: str
    stage: str          # OPENED | DRAFT_READY | USER_ACTION_REQUIRED | FORM_NOT_FOUND
    reason: str          # CAPTCHA_PRESENT | LOGIN_REQUIRED | UNRESOLVED_REQUIRED_FIELD | ...
    prepared_field_ids: list[str]
    unresolved_field_ids: list[str]
    resume_path: str
    cover_letter_path: str
```

This is exactly the "why stopped / what's needed / application URL / resume / fields
unresolved" record CLAUDE.md's user-action-handoff section asks for.

## Testing

`tests/test_application_browser_assist.py` is marked `@pytest.mark.browser` — never runs by
default (`pytest -m browser` to opt in), and skips automatically (rather than failing) if
Playwright or its chromium binary isn't actually launchable in the current environment. Every
test serves a local `file://` HTML fixture — no real website, no internet access, ever
required.

## Honest limitations (CLAUDE.md section 57)

- This MVP closes the browser before returning — it does not (yet) keep a visible window open
  for the candidate to continue typing into the same page. The `HandoffRecord` always carries
  the real `application_url` so the candidate can open it themselves and pick up where the
  scan left off.
- Field detection is a best-effort DOM scan (`<label for>`/`aria-label`/`placeholder`/`name`
  heuristics) — it will not correctly label every real-world form, especially heavily
  JavaScript-rendered ones with no accessible labels at all. When in doubt, a field is left
  unresolved rather than guessed.
- Browser form fill never implies permission to submit. This module's existence does not change
  any ATS's `submission_supported` capability declaration.

## Phase 11: apply-first-click and hardened sessions

The one-shot `prepare_application()` helper documented above is unchanged. The session-based API
(`app.applications.browser_assist.start_session()` and friends) gained a pre-form navigation
stage this phase -- see `docs/apply-entry-navigation.md` for the full mechanism and
`docs/browser-session-reconstruction.md` for the distributed-ownership hardening. Both remain
governed by the same safety boundaries listed above: no stealth, no CAPTCHA solving, never a
final submit click.

## Phase 12: SPA/dynamic hardening

The session-based API now also: waits for JS-rendered content via a bounded DOM-stabilization poll
instead of trusting `networkidle` (a genuinely SPA page may never reach it); follows a trusted
cross-domain redirect (career page -> recognized ATS vendor domain) where real evidence supports
it, not just a same-host apply-entry hop; discovers and fills fields inside an allowed-host iframe
or an open shadow root; and verifies the page still corresponds to the intended job before filling
a real form. None of this changes the safety boundaries above -- see
`docs/spa-application-navigation.md` and `docs/trusted-ats-redirects.md` for the full mechanism,
and `docs/phase12-spa-ats-hardening.md` for real, live-caught bugs this hardening surfaced.
