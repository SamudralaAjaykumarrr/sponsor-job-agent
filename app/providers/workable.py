import logging
import re
from html import unescape
from typing import Optional

import httpx

from app import config
from app.providers.base import JobProvider, RawJobPosting
from app.providers.capabilities import ProviderCapabilities, SupportLevel
from app.providers.http_client import ProviderHTTPError, build_client, get_json

logger = logging.getLogger("providers.workable")

# Public unauthenticated widget API -- the same one Workable's embeddable
# "apply" job list widget calls from a company's own careers page.
WORKABLE_LIST_URL = "https://apply.workable.com/api/v1/widget/accounts/{account}"
WORKABLE_DETAIL_URL = "https://apply.workable.com/api/v1/widget/accounts/{account}/jobs/{shortcode}"


def _strip_html(raw_html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw_html or "")
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _remote_status(item: dict) -> Optional[str]:
    if item.get("telecommute") is True:
        return "remote"
    city = (item.get("city") or "").lower()
    if "remote" in city:
        return "remote"
    return None


class WorkableProvider(JobProvider):
    """Public Workable widget API (apply.workable.com/api/v1/widget/accounts/<account>)
    -- unauthenticated. Paginates via `page`; bounded by
    MAX_PAGES_PER_PROVIDER/MAX_JOBS_PER_PROVIDER. Fetches the per-job detail
    endpoint (also public/unauthenticated) for the full description, bounded
    by max_jobs so total requests stay predictable."""

    name = "workable"
    capabilities = ProviderCapabilities(
        provider_name="workable",
        provider_version="1.0.0",
        discovery_supported=True,
        detail_fetch_supported=True,
        structured_location_supported=True,
        structured_published_at_supported=True,
        structured_salary_supported=False,
        structured_employment_type_supported=True,
        public_interface=True,
        requires_credentials=False,
        submission_supported=False,
        support_level=SupportLevel.FULL,
        notes="List endpoint paginated; description requires one detail request per job (bounded by max_jobs).",
    )

    def __init__(self, account_subdomains: list[str], client: Optional[httpx.Client] = None, timeout: float = 10.0):
        self.account_subdomains = account_subdomains
        self._client = client
        self._timeout = timeout

    def _fetch_list_page(self, client: httpx.Client, account: str, page: int) -> Optional[dict]:
        try:
            return get_json(client, WORKABLE_LIST_URL.format(account=account), provider="workable",
                             params={"page": page})
        except ProviderHTTPError as exc:
            logger.warning("workable account '%s' page %s fetch failed: %s", account, page, exc)
            self._last_error = exc
            return None
        except Exception as exc:
            logger.warning("workable account '%s' page %s fetch failed", account, page, exc_info=True)
            self._last_error = exc
            return None

    def _fetch_detail(self, client: httpx.Client, account: str, shortcode: str) -> str:
        if not shortcode:
            return ""
        try:
            detail = get_json(client, WORKABLE_DETAIL_URL.format(account=account, shortcode=shortcode),
                               provider="workable")
        except Exception:
            logger.warning("workable detail fetch failed for %s/%s", account, shortcode, exc_info=True)
            return ""
        parts = [detail.get("description", ""), detail.get("requirements", ""), detail.get("benefits", "")]
        return _strip_html(" ".join(p for p in parts if p))

    def _fetch_account(self, client: httpx.Client, account: str, max_jobs: int) -> list[RawJobPosting]:
        raw_items: list[dict] = []
        seen_shortcodes: set[str] = set()
        for page in range(1, config.MAX_PAGES_PER_PROVIDER + 1):
            data = self._fetch_list_page(client, account, page)
            if not data:
                break
            jobs = data.get("jobs") or []
            if not jobs:
                break
            new_on_page = 0
            for item in jobs:
                shortcode = item.get("shortcode") or item.get("code") or ""
                if shortcode and shortcode in seen_shortcodes:
                    continue  # repeated page-token safety
                if shortcode:
                    seen_shortcodes.add(shortcode)
                raw_items.append(item)
                new_on_page += 1
            if new_on_page == 0:
                break  # provider re-served the same page -- stop instead of looping
            if len(raw_items) >= max(max_jobs, config.MAX_JOBS_PER_PROVIDER):
                break

        results = []
        for item in raw_items[: config.MAX_JOBS_PER_PROVIDER]:
            try:
                shortcode = item.get("shortcode") or item.get("code") or ""
                description = self._fetch_detail(client, account, shortcode) if shortcode else ""
                url = item.get("url") or (f"https://apply.workable.com/{account}/j/{shortcode}/" if shortcode else "")
                results.append(RawJobPosting(
                    provider="workable",
                    external_job_id=shortcode or item.get("title", ""),
                    title=item.get("title", "") or "",
                    company=account.replace("-", " ").title(),
                    company_identifier=account,
                    location=", ".join(x for x in [item.get("city"), item.get("state"), item.get("country")] if x),
                    city=item.get("city"),
                    state=item.get("state"),
                    country=item.get("country"),
                    remote_status=_remote_status(item),
                    description=description,
                    url=url,
                    source_url=url,
                    employment_type_raw=item.get("employment_type", "") or "",
                    published_at=item.get("published_on"),
                    department=item.get("department"),
                    provider_metadata={"account": account, "shortcode": shortcode},
                ))
            except Exception:
                logger.warning("workable job normalize failed for account '%s'", account, exc_info=True)
                continue
        return results

    def fetch_jobs(self, max_jobs: int) -> list[RawJobPosting]:
        client = self._client or build_client(self._timeout)
        owns_client = self._client is None
        try:
            results: list[RawJobPosting] = []
            for account in self.account_subdomains:
                if len(results) >= max_jobs:
                    break
                for job in self._fetch_account(client, account, max_jobs - len(results)):
                    if len(results) >= max_jobs:
                        break
                    results.append(job)
            return results
        finally:
            if owns_client:
                client.close()
