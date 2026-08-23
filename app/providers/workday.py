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
      empty for that tenant) -- this is never treated as a bypass target."""

    name = "workday"
    capabilities = ProviderCapabilities(
        provider_name="workday",
        provider_version="1.1.0",
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
            "'Full time'/'Part time'); a tenant that omits this field simply leaves employment_type_raw empty."
        ),
    )

    def __init__(self, tenant_base_urls: list[str], client: Optional[httpx.Client] = None, timeout: float = 10.0):
        self.tenant_base_urls = tenant_base_urls
        self._client = client
        self._timeout = timeout

    def _fetch_detail(self, client: httpx.Client, base_url: str, external_path: str) -> tuple[str, str]:
        """Returns (description, employment_type_raw). `jobPostingInfo.timeType`
        (e.g. "Full time" / "Part time") is a genuine structured field on this
        endpoint -- live-verified against a real tenant, not guessed -- so it
        is surfaced as employment_type_raw rather than left for text-only
        fallback. A tenant/posting that omits the field yields "" here,
        exactly like every other optional field on this provider."""
        if not external_path:
            return "", ""
        try:
            resp = client.get(base_url + external_path)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            logger.warning("workday detail fetch failed for %s%s", base_url, external_path, exc_info=True)
            return "", ""
        info = data.get("jobPostingInfo") or {}
        return _strip_html(info.get("jobDescription", "")), (info.get("timeType", "") or "")

    def _fetch_tenant(self, client: httpx.Client, base_url: str, max_jobs: int) -> list[RawJobPosting]:
        tenant = _tenant_name(base_url)
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
                url = base_url.rstrip("/") + external_path if external_path else ""
                description, time_type = self._fetch_detail(client, base_url, external_path)
                results.append(RawJobPosting(
                    provider="workday",
                    external_job_id=str(item.get("jobPostingId") or item.get("bulletFields", [""])[0] or external_path),
                    title=item.get("title", "") or "",
                    company=tenant.replace("-", " ").title(),
                    company_identifier=tenant,
                    location=item.get("locationsText", "") or "",
                    description=description,
                    employment_type_raw=time_type,
                    url=url,
                    source_url=url,
                    provider_metadata={"tenant": tenant, "base_url": base_url, "posted_on_raw": item.get("postedOn")},
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
