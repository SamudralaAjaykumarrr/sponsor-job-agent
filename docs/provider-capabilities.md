# Provider Capability Model

Every provider connector (`app/providers/*.py`) declares a `ProviderCapabilities`
instance (`app/providers/capabilities.py`) as a class attribute. This is the single
source of truth the dashboard (`/providers`), tests, and this document all read from —
there is no separate "marketing" description of what a provider does; the capability
object *is* the description, and it must match the actual code.

## Fields

| Field | Meaning |
|---|---|
| `provider_name` | Matches `JobProvider.name` / the `ENABLED_PROVIDERS` value. |
| `provider_version` | Bumped when the connector's normalization logic changes materially. |
| `discovery_supported` | `fetch_jobs()` actually returns real postings from a live endpoint. |
| `detail_fetch_supported` | A per-job detail request is made/available to fill in the full description. |
| `structured_location_supported` | City/state/country/remote-status come from structured provider fields, not JD text guessing. |
| `structured_published_at_supported` | The provider gives a real parseable timestamp (not a relative string like "Posted 3 Days Ago"). |
| `structured_salary_supported` | Salary comes from a structured field, not regex-extracted from JD text. |
| `structured_employment_type_supported` | Employment type comes from a structured field. |
| `public_interface` | The endpoint is publicly reachable without login. |
| `requires_credentials` | An API key/OAuth/session is required (if `True`, this app will not fetch it — see Security below). |
| `submission_supported` | Always `False` for every provider in this codebase. This app never auto-submits applications, on any ATS, ever. |
| `support_level` | `FULL` / `PARTIAL` / `EXPERIMENTAL` / `UNSUPPORTED` (see below). |
| `notes` | Plain-language explanation of any limitation — required whenever anything is less than FULL. |

## Support levels

- **FULL** — discovery implemented, tested against realistic fixtures, expected to work
  broadly across tenants of that ATS. No known structural limitation.
- **PARTIAL** — discovery implemented but with a known, documented gap (e.g. missing
  description text, or configuration that can't be auto-derived per tenant).
- **EXPERIMENTAL** — implemented against a best-effort/unofficial pattern that has not
  been broadly verified and may silently stop working if the vendor changes their widget.
- **UNSUPPORTED** — no safe, reliable, public, unauthenticated interface was found. The
  provider still exists as a class (for detection/registry/dashboard uniformity) but
  `fetch_jobs()` always returns `[]` and never fabricates a result.

## Current matrix (as implemented — see `/providers` in the running dashboard for live values)

| Provider | Support | Why |
|---|---|---|
| Greenhouse | FULL | `boards-api.greenhouse.io` — public, single request per board, structured location/date. |
| Lever | FULL | `api.lever.co/v0/postings` — public, structured location/date/salary/employment type. |
| Ashby | FULL | Public Job Board API returns full description + structured fields in one call. |
| Workable | FULL | Public widget API; list is paginated, description needs one bounded detail call per job. |
| SmartRecruiters | FULL | Public Posting API; offset-paginated, description needs one bounded detail call per posting. |
| Recruitee | FULL | Public offers API returns full description in one call, no pagination needed. |
| Breezy HR | FULL | Public `/json` careers feed returns full description in one call. |
| BambooHR | PARTIAL | Discovery works, but there is no public JD detail endpoint — description is left empty, which correctly keeps sponsorship at `UNKNOWN` ("do not apply") instead of guessing. |
| Workday | PARTIAL | The CXS job-search endpoint is genuinely public (the same one the careers page's own frontend calls), but hosting number + site name vary per tenant and are not guessable — each tenant needs its exact base URL configured. `postedOn` is relative text, not a timestamp, so freshness always falls back to `first_seen_at` for this provider. Some tenants front the endpoint with bot protection this app will not attempt to bypass. |
| Comeet | EXPERIMENTAL | Requires a public per-company embed token that can't be derived from the company name alone; response schema is best-effort and unverified across tenants. |
| Teamtailor | UNSUPPORTED | The documented Careers API requires a partner API key; no verified stable unauthenticated public JSON endpoint was found. |
| Jobvite | UNSUPPORTED | No verified stable unauthenticated public JSON discovery endpoint across tenants. |
| Pinpoint | UNSUPPORTED | Same. |
| JazzHR | UNSUPPORTED | Public API requires an API key. |
| iCIMS | UNSUPPORTED | Career-site search endpoints vary heavily per tenant and commonly require session cookies/CSRF tokens issued by the search page itself — this app will not fabricate or replay those. |
| Oracle Recruiting Cloud | UNSUPPORTED | Site id/locale/finder parameters vary per tenant with no reliably guessable pattern. |

**Accuracy over count**: an UNSUPPORTED entry above is not a TODO to "just scrape it" —
it means no way was found to do it within this app's security rules (no anti-bot bypass,
no CAPTCHA bypass, no auth bypass, no credential theft). If a genuinely public,
unauthenticated interface is later found/verified for one of these, promote it — but only
after it is actually implemented and tested, never before.

## Programmatic access

```python
from app.providers.registry import all_capabilities, get_capabilities

all_capabilities()          # list[ProviderCapabilities] for every known provider
get_capabilities("ashby")   # ProviderCapabilities | None
```

The dashboard's `/providers` page renders `all_capabilities()` plus live tenant health
(from the company registry) — see `docs/company-registry.md`.
