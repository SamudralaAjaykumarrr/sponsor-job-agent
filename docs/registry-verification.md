# Registry Verification, Quality, and Lifecycle

## Lifecycle states (`app/registry/models.py::PortalStatus`)

```
DISCOVERED -> CANDIDATE -> VERIFIED -> ACTIVE
                  \-> QUARANTINED        \-> DEGRADED / STALE
                                          \-> QUARANTINED / DISABLED
```

Only **VERIFIED**/**ACTIVE** portals are ever mirrored into the operational `company_registry`
table that `app/agent/cycle.py` actually polls (`app/registry/sync.py`). Everything else is
inert data — it exists in the registry for review/analytics but is never polled.

`discovery_status` is a separate field recording *how* a candidate was found
(`IMPORTED`/`DETECTED`/`PAGE_DISCOVERY`/`MANUAL`) — independent of the lifecycle state above.

## Verification pipeline (`app/registry/verification.py`)

`verify_portal(portal, company_display_name=...)` runs two steps:

1. **Raw structural probe** (`app/registry/probe.py`) — a single bounded request to the specific
   provider's own public endpoint for this exact tenant, using the same URL templates, timeout,
   retry, and response-size-cap behavior as the real connectors
   (`app.providers.http_client.request_with_retries`), but — unlike `JobProvider.fetch_jobs()` —
   it **raises** on failure instead of swallowing it. (`fetch_jobs()` deliberately isolates
   per-tenant errors so one bad board never aborts a discovery cycle fetching many boards at once;
   verification needs the opposite — the raw, un-isolated outcome for exactly one tenant.) The
   probe's outcome alone determines VERIFIED vs. FAILED vs. TEMPORARY_FAILURE.
2. **Best-effort enrichment** — only once the probe has already succeeded, a normal
   `JobProvider.fetch_jobs()` call gets a normalized job count and a company-identity signal. Any
   failure here is informational only and never downgrades a verdict the probe already confirmed.

### Outcomes (`VerificationResult`)

| Result | Meaning | Effect on lifecycle |
|---|---|---|
| `VERIFIED` | Probe succeeded. | `DISCOVERED`/`CANDIDATE`/`STALE` → `VERIFIED`. `consecutive_permanent_failures` reset to 0. |
| `AMBIGUOUS` | Probe succeeded, but the company name observed in the response doesn't token-overlap with the registry company's name. | → `QUARANTINED`, never auto-activated. |
| `UNSUPPORTED` | No working discovery implementation for this provider (`SupportLevel.UNSUPPORTED`), or no structural probe exists yet. | No change — nothing can be checked either way. |
| `FAILED` | Permanent structural error (400/401/403/404/410). | `consecutive_permanent_failures += 1`; at `REGISTRY_STALE_AFTER_PERMANENT_FAILURES` (default 5), `VERIFIED`/`ACTIVE` → `STALE`, `DISCOVERED`/`CANDIDATE` → `QUARANTINED`. |
| `TEMPORARY_FAILURE` | Transient (429/5xx/timeout/connection error, or an unrecognized error shape — treated conservatively as temporary). | Recorded, but **never** counted toward permanent-failure demotion. A portal is never permanently discarded on one bad network moment. |

Never fabricates a tenant: if `portal.tenant_identifier` is empty, the outcome is `FAILED`
immediately, with no network call.

## Company identity safety

`_check_identity` token-compares `normalize_company_name(registry_company_name)` against
`normalize_company_name(job.company)` for each job the probe's enrichment step observed. Any
token overlap → `MATCHED`; some jobs observed but zero overlap → `MISMATCH` (→ `AMBIGUOUS`,
quarantined); no jobs observed at all → `UNKNOWN` (never fabricated as a match).

**Known limitation, observed in this session's live validation**: pure token-set comparison is
naive about concatenated tenant slugs — verifying `greenhouse/scaleai` against company name
"Scale AI" produced observed company `"Scaleai"` (tokens `{scaleai}`) vs. target tokens
`{scale, ai}` → no overlap → correctly flagged `AMBIGUOUS`/`QUARANTINED` even though it's the
right company. This is the safety check doing its job (never guess), at the cost of an
occasional false positive that needs one human click to confirm — a deliberate trade-off per
CLAUDE.md's "when identity cannot be established confidently: QUARANTINE, not ACTIVE."

## Quality/confidence score (`app/registry/quality.py`)

Deterministic, rule-based, each point tied to one auditable signal — never an opaque
probability:

| Signal | Points |
|---|---|
| Provenance from an official company-controlled source | 20 |
| Provider recognized (`FULL`/`PARTIAL` support level) | 15 |
| Tenant identifier extracted deterministically | 15 |
| Provider endpoint verified with a live response | 20 |
| Company identity matched | 15 |
| Recent successful poll | 8 |
| Recent jobs observed | 7 |

`confidence_reasons` (stored as JSON on the portal row, shown on the portal detail page) is the
literal list of which signals fired.

## Migration detection (`app/registry/lifecycle.py::maybe_detect_migration`)

A migration is recorded **only** when a company already has a `STALE` portal on a *different*
provider **and** a new portal for that company has *independently* reached
`VERIFIED`/`ACTIVE`. This is deliberately narrower than "a different provider showed up" — a
company legitimately running two ATSes at once (both healthy) never triggers a false migration
record (acceptance scenario E vs. F). On a real migration, the old portal is marked
`superseded_by_portal_id`, kept (never deleted), and a `registry_migrations` row preserves the
detected-at timestamp + evidence.

## Sync bridge (`app/registry/sync.py`)

`sync_portal_to_operational_registry(portal_id)` is the only integration point with Phase 3's
unchanged `company_registry` table: it upserts (by the existing `(provider, tenant_identifier)`
unique index) when the portal is `VERIFIED`/`ACTIVE`+enabled, promotes the portal to `ACTIVE`, and
records the mirrored row's id back on `registry_portals.registry_entry_id`. When the portal
regresses (`STALE`/`QUARANTINED`/`DISABLED`/`DEGRADED`), the mirrored row is **disabled**
(`enabled=False`), never deleted — poll history and provenance survive.
