import logging
import re
from html import unescape
from typing import Optional
from urllib.parse import urlparse

import httpx

from app import config
from app.providers.base import JobProvider, RawJobPosting
from app.providers.capabilities import ProviderCapabilities, SupportLevel
from app.providers.http_client import ProviderHTTPError, build_client, post_json

logger = logging.getLogger("providers.workday")

PAGE_LIMIT = 20


def _strip_html(raw_html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw_html or "")
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _tenant_name(base_url: str) -> str:
    host = urlparse(base_url).netloc
    return host.split(".")[0] if host else base_url


_CXS_BASE_PATH_RE = re.compile(r"^/wday/cxs/(?P<tenant>[^/]+)/(?P<site>[^/]+)/?$", re.I)


def _candidate_base(base_url: str) -> str:
    """Derives the real candidate-facing base (scheme://host/{site}) from the
    CXS API base_url (scheme://host/wday/cxs/{tenant}/{site}) each tenant is
    configured with. These are DIFFERENT paths on the same host -- appending
    externalPath directly to the API base (this provider's previous
    behavior) produces a URL that coincidentally also resolves, but to the
    raw jobPostingInfo JSON detail endpoint (the exact same URL
    _fetch_detail() requests), not the real HTML application page. That
    silently broke every downstream browser-assist/application step for
    every Workday job that went through this provider, since the URL never
    exercised job_identity/browser_runtime against real page content.
    Live-verified 2026-08-22 against a real tenant
    (walmart.wd504.myworkdayjobs.com/WalmartExternal): the CXS-base URL
    returns bare JSON with no page title; host+'/'+site+path returns the
    real rendered page (title, 'Apply' button, requisition id visible).
    Falls back to the API base unchanged only if base_url doesn't match the
    documented CXS shape -- never guessed."""
    parsed = urlparse(base_url)
    match = _CXS_BASE_PATH_RE.match(parsed.path or "")
    if not match:
        return base_url.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}/{match.group('site')}"


class WorkdayProvider(JobProvider):
    """Workday CXS job-search API -- the same unauthenticated POST endpoint a
    public Workday careers site's own frontend calls
    (https://{tenant}.wdN.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs).

    PARTIAL support, by design:
    - Workday hosting numbers (wd1, wd5, ...) and site names vary per tenant
      and are NOT guessable from a company name -- each tenant must be
      configured with its FULL base URL (WORKDAY_TENANT_BASE_URLS). We never
      invent a tenant's hosting number.
    - `postedOn` is a relative human string ("Posted 3 Days Ago"), not a
      parseable timestamp -- published_at is left None and freshness falls
      back to first_seen_at (freshness_source=FIRST_SEEN) rather than
      fabricating an absolute date from a relative one.
    - Some tenants front this endpoint with bot protection that a plain
      httpx client cannot pass; those tenants fail cleanly (logged, job list
      empty for that tenant) -- this is never treated as a bypass target.
    - `url`/`source_url` are the real candidate-facing page
      (scheme://host/{site}{externalPath}), NOT the CXS API base + path --
      see _candidate_base()'s docstring for the real bug this fixed
      (2026-08-22): the API base and the candidate page live at different
      paths on the same host, and appending externalPath to the API base
      coincidentally also resolved, but to raw JSON, not the real HTML
      application form."""

    name = "workday"
    capabilities = ProviderCapabilities(
        provider_name="workday",
        provider_version="1.2.0",
        discovery_supported=True,
        detail_fetch_supported=True,
        structured_location_supported=True,
        structured_published_at_supported=False,
        structured_salary_supported=False,
        structured_employment_type_supported=True,
        public_interface=True,
        requires_credentials=False,
        submission_supported=False,
        support_level=SupportLevel.PARTIAL,
        notes=(
            "Requires each tenant's exact base URL (hosting number + site name are not guessable); "
            "postedOn is relative text, not a timestamp, so freshness falls back to first_seen_at; "
            "some tenants front this endpoint with bot protection this client will not attempt to bypass. "
            "employment_type_raw is populated from the per-job detail endpoint's jobPostingInfo.timeType "
            "field (live-verified against walmart.wd504.myworkdayjobs.com/WalmartExternal -- e.g. "
            "'Full time'/'Part time'); a tenant that omits this field simply leaves employment_type_raw empty. "
            "url/source_url are the real candidate-facing page (host + '/' + site + externalPath), fixed "
            "2026-08-22 -- previously appended externalPath to the CXS API base instead, which resolved to "
            "raw JSON, not the application form (live-verified against the same tenant). "
            "provider_metadata['job_req_id'] and structured `country` are populated from the same detail "
            "endpoint's jobPostingInfo.jobReqId / jobPostingInfo.country.descriptor when present; job_req_id "
            "is metadata only, never used for external_job_id (which must stay derived solely from the list "
            "response so it never flips if a later detail fetch fails). No structured city/state field exists "
            "on this API -- only a combined `location` string -- so those stay unset rather than guessed."
        ),
    )

    def __init__(self, tenant_base_urls: list[str], client: Optional[httpx.Client] = None, timeout: float = 10.0):
        self.tenant_base_urls = tenant_base_urls
        self._client = client
        self._timeout = timeout

    _EMPTY_DETAIL = {
        "description": "", "employment_type_raw": "", "job_req_id": "", "country": None,
        "can_apply": None, "external_url": "",
    }

    def _fetch_detail(self, client: httpx.Client, base_url: str, external_path: str) -> dict:
        """Returns {"description", "employment_type_raw", "job_req_id", "country",
        "can_apply", "external_url"} from the per-job detail endpoint.
        `jobPostingInfo.timeType` (e.g. "Full time" / "Part time") and
        `jobPostingInfo.jobReqId` (the stable requisition id, e.g.
        "R-1826704") are genuine structured fields -- live-verified against
        real tenants (walmart.wd504.myworkdayjobs.com), not guessed.
        `jobPostingInfo.country.descriptor` is likewise a real structured
        field (e.g. "Canada"); Workday does NOT expose a separate structured
        state/city -- only a human-readable `location`/
        `jobRequisitionLocation.descriptor` string that bundles them, which
        is why `location` stays a combined string rather than being split
        into city/state here. `jobPostingInfo.canApply` (bool) is a genuine
        structured signal for whether the posting currently accepts
        applications -- live-verified True on an open posting; a tenant
        that closes/withdraws a requisition without removing it from search
        results is exactly the case this exists to catch, so it is
        surfaced (never fabricated when absent -> None, not False).
        `jobPostingInfo.externalUrl` is the tenant's own authoritative
        candidate-facing URL -- live-verified to equal this provider's own
        `_candidate_base()`-constructed URL byte-for-byte on a real posting;
        surfaced here purely so `_fetch_tenant` can cross-check and WARN
        (never silently override) if a future tenant's externalUrl ever
        diverges from the constructed one. A tenant/posting that omits any
        of these fields yields "" / None here, exactly like every other
        optional field on this provider. `jobPostingInfo.startDate` is
        deliberately NEVER read -- live-verified to be a job/requisition
        start date unrelated to posting recency (a real posting shown as
        "Posted 30+ Days Ago" carried a startDate over two years in the
        past), so treating it as published_at would fabricate freshness."""
        if not external_path:
            return dict(self._EMPTY_DETAIL)
        try:
            resp = client.get(base_url + external_path)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            logger.warning("workday detail fetch failed for %s%s", base_url, external_path, exc_info=True)
            return dict(self._EMPTY_DETAIL)
        info = data.get("jobPostingInfo") or {}
        country = ((info.get("country") or {}).get("descriptor")) or None
        can_apply = info.get("canApply")
        return {
            "description": _strip_html(info.get("jobDescription", "")),
            "employment_type_raw": info.get("timeType", "") or "",
            "job_req_id": info.get("jobReqId", "") or "",
            "country": country,
            "can_apply": can_apply if isinstance(can_apply, bool) else None,
            "external_url": info.get("externalUrl", "") or "",
        }

    def _fetch_tenant(self, client: httpx.Client, base_url: str, max_jobs: int) -> list[RawJobPosting]:
        tenant = _tenant_name(base_url)
        candidate_base = _candidate_base(base_url)
        postings: list[dict] = []
        offset = 0
        total: Optional[int] = None
        for _page in range(config.MAX_PAGES_PER_PROVIDER):
            try:
                data = post_json(
                    client, base_url.rstrip("/") + "/jobs", provider="workday",
                    json_body={"appliedFacets": {}, "limit": PAGE_LIMIT, "offset": offset, "searchText": ""},
                )
            except ProviderHTTPError as exc:
                logger.warning("workday tenant '%s' offset %s fetch failed: %s", tenant, offset, exc)
                self._last_error = exc
                break
            except Exception as exc:
                logger.warning("workday tenant '%s' offset %s fetch failed", tenant, offset, exc_info=True)
                self._last_error = exc
                break

            total = data.get("total", total)
            page = data.get("jobPostings") or []
            if not page:
                break
            postings.extend(page)
            offset += len(page)
            if len(postings) >= max(max_jobs, config.MAX_JOBS_PER_PROVIDER):
                break
            if total is not None and offset >= total:
                break

        results = []
        for item in postings[: config.MAX_JOBS_PER_PROVIDER]:
            try:
                external_path = item.get("externalPath", "") or ""
                url = candidate_base + external_path if external_path else ""
                detail = self._fetch_detail(client, base_url, external_path)
                job_req_id = detail["job_req_id"]
                metadata = {"tenant": tenant, "base_url": base_url, "posted_on_raw": item.get("postedOn")}
                if job_req_id:
                    metadata["job_req_id"] = job_req_id
                if detail["can_apply"] is not None:
                    metadata["can_apply"] = detail["can_apply"]
                if detail["external_url"] and url and detail["external_url"] != url:
                    # Cross-check only, never a silent override: the
                    # constructed URL stays authoritative (always available
                    # even when the detail fetch fails), but a genuine
                    # divergence from Workday's own reported externalUrl is
                    # exactly the kind of thing that should be visible, not
                    # swallowed.
                    logger.warning(
                        "workday tenant '%s' externalUrl mismatch: constructed=%s reported=%s",
                        tenant, url, detail["external_url"],
                    )
                    metadata["reported_external_url"] = detail["external_url"]
                results.append(RawJobPosting(
                    provider="workday",
                    # NEVER prefer job_req_id here even though it's the more
                    # "canonical" id -- it only comes from the per-job detail
                    # fetch, which can fail independently of the list fetch
                    # (network blip, tenant rate limit). Letting external_job_id
                    # depend on detail-fetch success would make the SAME job's
                    # id flip between runs, breaking dedup/tracking stability
                    # (see CLAUDE.md's cross-provider dedup rule) -- the list
                    # response's own jobPostingId/bulletFields is always
                    # present whenever the job appears at all, so it stays the
                    # sole source of truth for identity.
                    external_job_id=str(item.get("jobPostingId") or item.get("bulletFields", [""])[0] or external_path),
                    title=item.get("title", "") or "",
                    company=tenant.replace("-", " ").title(),
                    company_identifier=tenant,
                    location=item.get("locationsText", "") or "",
                    country=detail["country"],
                    description=detail["description"],
                    employment_type_raw=detail["employment_type_raw"],
                    url=url,
                    source_url=url,
                    provider_metadata=metadata,
                ))
            except Exception:
                logger.warning("workday job normalize failed for tenant '%s'", tenant, exc_info=True)
                continue
        return results

    def fetch_jobs(self, max_jobs: int) -> list[RawJobPosting]:
        client = self._client or build_client(self._timeout)
        owns_client = self._client is None
        try:
            results: list[RawJobPosting] = []
            for base_url in self.tenant_base_urls:
                if len(results) >= max_jobs:
                    break
                for job in self._fetch_tenant(client, base_url, max_jobs - len(results)):
                    if len(results) >= max_jobs:
                        break
                    results.append(job)
            return results
        finally:
            if owns_client:
                client.close()
