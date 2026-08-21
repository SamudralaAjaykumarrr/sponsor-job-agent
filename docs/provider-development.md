# Adding a Provider Connector

This extends the Phase 2 "Adding a new provider" note in `docs/autonomous-agent.md` with
the Phase 3 requirements (capability model, hardened HTTP, pagination safety, testing).

## 1. Only public, unauthenticated, ToS-respecting interfaces

Before writing any code, confirm the endpoint is:

- Publicly reachable without login/API key/session cookie.
- The same endpoint the ATS's own public careers-page frontend calls (i.e. you are not
  discovering a private/internal API).
- Not fronted by bot protection you'd need to defeat to get a response.

If you can't confirm this, don't implement discovery — add the provider to
`app/providers/unsupported.py` instead, with `support_level=UNSUPPORTED` and a `notes`
string explaining exactly why (see existing entries for the pattern).

## 2. Implement the connector

Create `app/providers/<name>.py`:

```python
from app.providers.base import JobProvider, RawJobPosting
from app.providers.capabilities import ProviderCapabilities, SupportLevel
from app.providers.http_client import ProviderHTTPError, build_client, get_json

class MyATSProvider(JobProvider):
    name = "myats"
    capabilities = ProviderCapabilities(
        provider_name="myats", provider_version="1.0.0",
        discovery_supported=True, detail_fetch_supported=False,
        structured_location_supported=True, structured_published_at_supported=True,
        structured_salary_supported=False, structured_employment_type_supported=True,
        public_interface=True, requires_credentials=False, submission_supported=False,
        support_level=SupportLevel.FULL, notes="...",
    )

    def __init__(self, tenant_ids: list[str], client=None, timeout: float = 10.0):
        self.tenant_ids = tenant_ids
        self._client = client
        self._timeout = timeout

    def fetch_jobs(self, max_jobs: int) -> list[RawJobPosting]:
        client = self._client or build_client(self._timeout)
        owns_client = self._client is None
        try:
            ...  # isolate every per-tenant error internally (try/except, log + continue)
        finally:
            if owns_client:
                client.close()
```

Rules, in order of importance:

1. **Never fabricate a field.** If the ATS doesn't expose it, leave it `None`/`""` on
   `RawJobPosting` — never guess, never regex a value the provider didn't actually give you
   and present it as structured.
2. **Isolate errors per tenant.** One bad board/company/subdomain must never raise out of
   `fetch_jobs()` — catch, log a `logger.warning(...)`, and continue with the next tenant.
3. **Route all HTTP through `app/providers/http_client.py`** (`build_client`, `get_json`,
   `post_json`, or `request_with_retries` directly) — this is what gives you bounded
   timeouts, bounded retries with backoff, the response-size cap, and the shared
   `PROVIDER_USER_AGENT`. Don't build your own `httpx.Client()` or retry loop.
4. **Respect pagination limits.** Loop at most `config.MAX_PAGES_PER_PROVIDER` pages, cap
   total results at `config.MAX_JOBS_PER_PROVIDER`, and stop immediately if a page comes
   back empty or repeats already-seen IDs (protects against broken/malicious pagination —
   see `WorkableProvider._fetch_account` for the reference pattern).
5. **Accept a `client: Optional[httpx.Client]` and `timeout` constructor arg** so tests can
   inject an `httpx.MockTransport`-backed client without touching the network.
6. **Set `RawJobPosting.source_url`** (and `url`) so cross-provider dedup / canonicalization
   (`app/discovery/dedup.py::canonicalize_url`) can work — this is what lets the same
   requisition syndicated from two sources collapse into one job with two provenance rows.

## 3. Register it

- Add a factory entry to `_PROVIDER_FACTORIES` in `app/providers/registry.py` (reads tenant
  identifiers from a new `config.<NAME>_...` list).
- Add the same class to `_PROVIDER_CLASSES` so capabilities/tenant-building work uniformly.
- Add the config list + a comment in `.env.example`.
- If the provider needs special single-tenant construction (like Workday/Comeet), add a
  branch in `build_provider_for_tenant()`.

## 4. Add detection support (optional but expected)

Add a `_rule_<name>` function to `app/providers/detector.py` matching the ATS's known
public URL host/path pattern, returning a `DetectionResult` with a tenant identifier only
when it's deterministically extractable from the URL. Never report high confidence for a
bare host match with no tenant.

## 5. Tests — all required, no live network

Every provider needs, using `httpx.MockTransport` fixtures (see `tests/test_ashby_provider.py`
for the template):

- Normalization of a realistic success response.
- Per-tenant error isolation (one tenant 500s/times out, another succeeds — only the good
  one's jobs come back).
- Malformed/unexpected payload shape doesn't raise out of `fetch_jobs()`.
- `max_jobs` is respected.
- Pagination termination (if paginated): stops on an empty page and never loops forever on
  a provider that keeps re-serving the same page.
- `capabilities.support_level` matches what's documented in `docs/provider-capabilities.md`.

Run `pytest -q` — all existing tests (Phase 2 + prior Phase 3 connectors) must still pass;
a new connector must never change another provider's behavior.

## 6. Update docs

- `docs/provider-capabilities.md` — add a row to the matrix.
- `.env.example` — add the tenant-list variable with a one-line comment.
- If it changes cross-cutting architecture (new DB columns, new dedup rule, etc.), also
  update `docs/architecture.md`.
