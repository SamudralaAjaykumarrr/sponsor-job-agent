# SPA / Dynamic Application Navigation

How `app.applications.browser_runtime` handles JS-rendered/single-page-app ATS flows
(CLAUDE.md Phase 12 sections 10-15, 30-40).

## Bounded DOM stabilization

`_wait_for_stable_state(page)` replaces a blind `page.wait_for_load_state("networkidle")` (which a
genuinely SPA-rendered page may never reach -- it can keep issuing background XHR/websocket
traffic indefinitely). It polls, at most `BROWSER_DOM_STABILIZATION_TIMEOUT_MS` (default 8000ms,
polling every `BROWSER_DOM_STABILIZATION_POLL_MS`, default 250ms), for whichever comes first:

1. **`content_ready`** -- a password field or an ordinary fillable field appeared.
2. **`dom_stable`** -- the page's own DOM size signature (`document.documentElement.outerHTML.length`)
   held steady across `BROWSER_DOM_STABILIZATION_SETTLE_POLLS` (default 3) consecutive polls, even
   if nothing recognizable ever appeared (e.g. a plain job-description landing page with no form).
3. **`timeout`** -- neither happened within the bound.

Called after every navigation that might trigger client-side rendering: the initial `goto()`
(`_do_open`), and after every apply-entry/next-step click (`_do_advance_to_route`).

## SPA route-change detection

`_do_advance_to_route(current_url_before)` compares `page.url` before and after the stabilization
wait. A changed URL with no full page load (pushState/hashchange/History API) is recorded as
`spa_route_detected` in `app.applications.spa_events`. This is deliberately just an observation --
the ordinary rediscovery pass that follows already re-scans the CURRENT page state regardless of
whether the URL changed, an SPA route change, or nothing changed at all.

## Trusted-redirect-aware apply-entry classification

See `docs/trusted-ats-redirects.md` for the full trust model. In short: `apply_entry.
classify_apply_control_detailed()` now checks the destination host's trust before falling through
to text classification -- an `EXTERNAL_REDIRECT` is only assigned when the destination is neither
the current host NOR a recognized ATS vendor domain.

## Multiple apply controls / ambiguity

`apply_entry.select_apply_control(candidates)` resolves the DOM scan's full candidate list:

- Multiple `NAVIGATION_SAFE` candidates sharing the SAME destination (top/bottom/sticky Apply
  buttons, all pointing at the same form) are not ambiguous -- the first is used.
- Multiple `NAVIGATION_SAFE` candidates with DIFFERENT destinations (e.g. a "similar jobs" Apply
  button elsewhere on the page) are never resolved by guessing -- the session pauses
  `PAUSED_AMBIGUOUS_APPLY_CONTROL` for a human to pick.

## Iframe discovery

`_scan_iframes(page, provider, original_url)` enumerates every frame Playwright can normally read
(the same access a browser's own devtools has -- never a cross-origin sandbox bypass):

- A frame whose host is on the session's domain allowlist (`app.applications.domain_allowlist.
  is_allowed_host_for_session`) has its fields (and submit/next button, if present) folded into
  the main discovery pass. Each field dict is tagged with its source `Frame` object so filling
  targets the right document -- a real live test caught fields being discovered but silently never
  filled before this tagging was added (see `docs/phase12-spa-ats-hardening.md`'s bug list).
- A frame whose host is NOT allowed only pauses the session (`PAUSED_IFRAME_UNEXPECTED_HOST`) when
  it actually contains form-shaped content -- an ad/analytics/tracking iframe (ubiquitous on real
  career pages) never triggers a pause by itself.
- In the real production discovery path, the existing page-content CAPTCHA check runs BEFORE the
  iframe scan, so a CAPTCHA-widget iframe (which can itself contain a stray form-like element) is
  caught by `PAUSED_CAPTCHA` first, never misclassified as an unexpected-host iframe.

## Shadow DOM discovery

Every DOM-scanning `page.evaluate()` call in `browser_runtime` (`_detect_fields`, `_detect_button`,
`_detect_apply_entry_control`) uses a shared recursive `__deepQueryAll()` helper
(`_DEEP_QUERY_JS`) that walks into any element's `.shadowRoot` when present. This only ever finds
OPEN shadow roots -- `el.shadowRoot` is `null`/`undefined` for a closed one, so a closed root's
contents are genuinely undiscoverable, which is the correct honest `UNSUPPORTED` outcome, never a
bypass attempt (see `tests/browser_fixtures.py`'s `closed_shadow_dom_form_page` and its test).

## Job-identity verification

`app.applications.job_identity.verify_job_identity(original_url, current_url)` extracts a
requisition/posting-id-shaped token from each URL (Workday's `_R-1234` suffix, or a
`gh_jid`/`jobId`/`job`/`req`-style query parameter) and compares them. Only a CONFIDENT mismatch
(a token was extractable from BOTH URLs and they differ) pauses the session
(`PAUSED_JOB_IDENTITY_MISMATCH`) -- when no confident token exists on one or both URLs, the result
is `UNVERIFIABLE`, never a guessed match or mismatch. Checked in `_do_discover()` right before a
real form would be filled.

## SPA form drift vs. genuine step progression

`browser_assist._apply_discovery_outcome`'s `check_drift` flag (unchanged from Phase 10/11) already
distinguishes an intentional multi-step advance (fields legitimately entirely different, not
checked for drift) from an unexpected mid-pause form change (checked, pauses
`PAUSED_FORM_CHANGED`). This principle extends unchanged to SPA route changes: a route change
triggered by this project's own apply-entry/next-step click is expected and not drift-checked; an
unexpected form change observed on `resume_session()` (the browser was left alone and came back
different) still is.
