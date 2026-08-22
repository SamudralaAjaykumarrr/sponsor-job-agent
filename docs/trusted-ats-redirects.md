# Trusted ATS Redirects

The deterministic model behind "when is it safe to follow an apply-entry control whose
destination is a DIFFERENT host than the current page" (CLAUDE.md Phase 12 sections 8-9, 26-27,
63). Implemented in `app.applications.trusted_redirects`.

## The problem Phase 11 left open

A real employer career page's Apply link commonly points at a DIFFERENT host than the career page
itself -- the employer's chosen ATS vendor domain (`boards.greenhouse.io`, `jobs.lever.co`, ...).
Phase 11's `classify_apply_control` treated ANY cross-host destination as `EXTERNAL_REDIRECT`
(never clicked), which is safe but overly conservative: it meant this project's apply-first-click
mechanism could never actually follow the most common real-world shape (career page -> ATS
domain), only the less common same-host case.

## The model

`classify_redirect_trust(current_host, href)` returns one of:

- **`SAME_HOST`** -- the destination is the current page itself (relative href, or an absolute
  href on the same host), or a `file://` URL (this project's entire local test-fixture mechanism --
  see the "real, live-caught bugs" note below).
- **`TRUSTED_ATS_REDIRECT`** -- the destination host matches one of the SAME per-provider domain
  suffixes `app.applications.domain_allowlist.PROVIDER_DOMAINS` already uses for post-navigation
  host checks (excluding `mock_ats`'s local/test hosts, which must never be a real trust signal).
  Reusing this existing, already-vetted table (rather than building a second, parallel one) is
  deliberate -- CLAUDE.md section 9 explicitly forbids a broad "any external link is fine"
  allowlist; trust here requires the SAME domain evidence this project already trusts elsewhere.
- **`UNTRUSTED`** -- a cross-host destination that matches no known ATS vendor domain. Still
  classified `EXTERNAL_REDIRECT` by `apply_entry.classify_apply_control`, exactly as Phase 11
  already did -- never clicked.
- **`UNSAFE_SCHEME`** -- `javascript:`/`data:`/`vbscript:` (or any scheme outside the safe set).
  Always `UNKNOWN`, never clicked, regardless of visible text.

`apply_entry.classify_apply_control_detailed()` uses this: an `UNTRUSTED` or `UNSAFE_SCHEME`
destination short-circuits to `EXTERNAL_REDIRECT`/`UNKNOWN` before text is even considered (same
priority Phase 11 already had for "any" cross-host mismatch). A `TRUSTED_ATS_REDIRECT` or
`SAME_HOST` destination falls through to ordinary text classification -- so a `TRUSTED_ATS_REDIRECT`
control whose text says "Apply Now" becomes `NAVIGATION_SAFE` (may be clicked), while one whose
text says "Submit Application" is still `FINAL_SUBMIT` (never clicked) -- trust only ever unlocks
the text-classification path, never final-submit safety.

## Live proof (not merely unit-tested)

`scripts/phase12_live_validation.py::validate_gitlab_career_page_trusted_redirect()` opens GitLab's
own CORPORATE careers page (`about.gitlab.com/jobs/all-jobs/` -- not a `greenhouse.io` domain at
all) and classifies every real link it finds pointing at a recognized ATS vendor domain. Result
from a real run this phase:

```
current_host: about.gitlab.com
ats_links_found: 10
trusted_count: 10
```

All 10 real `job-boards.greenhouse.io/gitlab/jobs/<id>` links classified `TRUSTED_ATS_REDIRECT`.
This is the first genuine real-world (not merely fixture/unit-tested) proof that the
career-page-to-ATS-domain trust model works against an actual, unrelated company's actual
production careers page.

## Application-URL provenance

`resolve_application_url(canonical_url, job_url, provider)` picks which URL a browser-assist
session actually opens, in priority order:

1. `DISCOVERY_PROVIDER` -- the discovery-time provider adapter's own `canonical_url`, when set
   (Greenhouse/Lever/Ashby/Workable postings already resolve directly to the real form for most
   tenants observed so far).
2. `JOB_DETAIL` -- the more generic `job.url` (often the career-portal listing page, needing an
   apply-entry hop).
3. `USER_PROVIDED` -- neither was available; a human must supply one.

Recorded on `browser_assist_sessions.url_provenance` for audit -- never used to relax any safety
check, purely descriptive.

## A real, live-caught bug

An earlier version of `classify_redirect_trust` treated `file://` as an `UNSAFE_SCHEME`. This
project's ENTIRE local browser-fixture test suite (`tests/browser_fixtures.py`) uses `file://`
URLs for every fixture page, including cross-page links between fixtures (e.g. a landing page's
"Apply Now" link points at another `file://` HTML file on disk). A real live-Chromium run of the
Phase 11 regression suite caught every apply-entry test failing immediately after this module was
wired into `apply_entry.classify_apply_control`. Fixed by adding the exact same `file://`
carve-out `app.applications.domain_allowlist.is_allowed_domain` already established
(`if scheme == "file": return True`) -- `file://` is never a real navigation target, only this
project's own local test mechanism.

## Safety

`app.applications.doctor._check_unsafe_redirect_allowlist` statically asserts every trusted suffix
is a real, specific domain (never a bare/near-empty string, never a generic TLD) -- a broad entry
here would silently turn this into the "any external link is fine" allowlist section 9 forbids.
