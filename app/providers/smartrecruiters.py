import logging
import re
from html import unescape
from typing import Optional

import httpx

from app import config
from app.providers.base import JobProvider, RawJobPosting
from app.providers.capabilities import ProviderCapabilities, SupportLevel
from app.providers.http_client import ProviderHTTPError, build_client, get_json

logger = logging.getLogger("providers.smartrecruiters")

SMARTRECRUITERS_LIST_URL = "https://api.smartrecruiters.com/v1/companies/{company}/postings"
SMARTRECRUITERS_DETAIL_URL = "https://api.smartrecruiters.com/v1/companies/{company}/postings/{posting_id}"
PAGE_LIMIT = 100


def _strip_html(raw_html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw_html or "")
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _remote_status(location: dict) -> Optional[str]:
    if location.get("remote") is True:
        return "remote"
    return None


class SmartRecruitersProvider(JobProvider):
    """Public SmartRecruiters Posting API
    (api.smartrecruiters.com/v1/companies/<company>/postings) -- unauthenticated.
    Paginated via offset/limit; bounded by MAX_PAGES_PER_PROVIDER /
    MAX_JOBS_PER_PROVIDER. Full description requires a per-posting detail
    request, bounded by max_jobs."""

    name = "smartrecruiters"
    capabilities = ProviderCapabilities(
        provider_name="smartrecruiters",
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
        notes="List endpoint offset-paginated; description requires one detail request per posting.",
    )

    def __init__(self, company_ids: list[str], client: Optional[httpx.Client] = None, timeout: float = 10.0):
        self.company_ids = company_ids
        self._client = client
        self._timeout = timeout

    def _fetch_page(self, client: httpx.Client, company: str, offset: int) -> Optional[dict]:
        try:
            return get_json(client, SMARTRECRUITERS_LIST_URL.format(company=company), provider="smartrecruiters",
                             params={"limit": PAGE_LIMIT, "offset": offset})
        except ProviderHTTPError as exc:
            logger.warning("smartrecruiters company '%s' offset %s fetch failed: %s", company, offset, exc)
            return None
        except Exception:
            logger.warning("smartrecruiters company '%s' offset %s fetch failed", company, offset, exc_info=True)
            return None

    def _fetch_description(self, client: httpx.Client, company: str, posting_id: str) -> str:
        if not posting_id:
            return ""
        try:
            detail = get_json(client, SMARTRECRUITERS_DETAIL_URL.format(company=company, posting_id=posting_id),
                               provider="smartrecruiters")
        except Exception:
            logger.warning("smartrecruiters detail fetch failed for %s/%s", company, posting_id, exc_info=True)
            return ""
        sections = ((detail.get("jobAd") or {}).get("sections") or {})
        parts = [sections.get(k, {}).get("text", "") for k in ("jobDescription", "qualifications", "additionalInformation")]
        return _strip_html(" ".join(p for p in parts if p))

    def _fetch_company(self, client: httpx.Client, company: str, max_jobs: int) -> list[RawJobPosting]:
        postings: list[dict] = []
        offset = 0
        total_found: Optional[int] = None
        for _page in range(config.MAX_PAGES_PER_PROVIDER):
            data = self._fetch_page(client, company, offset)
            if not data:
                break
            total_found = data.get("totalFound", total_found)
            content = data.get("content") or []
            if not content:
                break
            postings.extend(content)
            offset += len(content)
            if len(postings) >= max(max_jobs, config.MAX_JOBS_PER_PROVIDER):
                break
            if total_found is not None and offset >= total_found:
                break

        results = []
        for item in postings[: config.MAX_JOBS_PER_PROVIDER]:
            try:
                posting_id = str(item.get("id", ""))
                location = item.get("location") or {}
                department = (item.get("department") or {}).get("label")
                employment = (item.get("typeOfEmployment") or {}).get("label", "") or ""
                results.append(RawJobPosting(
                    provider="smartrecruiters",
                    external_job_id=posting_id,
                    title=item.get("name", "") or "",
                    company=(item.get("company") or {}).get("name") or company,
                    company_identifier=company,
                    location=", ".join(x for x in [location.get("city"), location.get("region"), location.get("country")] if x),
                    city=location.get("city"),
                    state=location.get("region"),
                    country=location.get("country"),
                    remote_status=_remote_status(location),
                    description=self._fetch_description(client, company, posting_id),
                    url=item.get("postingUrl", "") or "",
                    source_url=item.get("postingUrl", "") or "",
                    employment_type_raw=employment,
                    published_at=item.get("releasedDate"),
                    department=department,
                    provider_metadata={"company_identifier": company, "posting_id": posting_id},
                ))
            except Exception:
                logger.warning("smartrecruiters job normalize failed for company '%s'", company, exc_info=True)
                continue
        return results

    def fetch_jobs(self, max_jobs: int) -> list[RawJobPosting]:
        client = self._client or build_client(self._timeout)
        owns_client = self._client is None
        try:
            results: list[RawJobPosting] = []
            for company in self.company_ids:
                if len(results) >= max_jobs:
                    break
                for job in self._fetch_company(client, company, max_jobs - len(results)):
                    if len(results) >= max_jobs:
                        break
                    results.append(job)
            return results
        finally:
            if owns_client:
                client.close()
