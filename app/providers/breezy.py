import logging
import re
from html import unescape
from typing import Optional

import httpx

from app.providers.base import JobProvider, RawJobPosting
from app.providers.capabilities import ProviderCapabilities, SupportLevel
from app.providers.http_client import ProviderHTTPError, build_client, get_json

logger = logging.getLogger("providers.breezy")

BREEZY_LIST_URL = "https://{subdomain}.breezy.hr/json"


def _strip_html(raw_html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw_html or "")
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


class BreezyProvider(JobProvider):
    """Public Breezy HR careers JSON feed (<subdomain>.breezy.hr/json) --
    unauthenticated, returns every open position with full description in one
    response; no pagination needed."""

    name = "breezy"
    capabilities = ProviderCapabilities(
        provider_name="breezy",
        provider_version="1.0.0",
        discovery_supported=True,
        detail_fetch_supported=False,
        structured_location_supported=True,
        structured_published_at_supported=True,
        structured_salary_supported=False,
        structured_employment_type_supported=True,
        public_interface=True,
        requires_credentials=False,
        submission_supported=False,
        support_level=SupportLevel.FULL,
        notes="Single unauthenticated request per subdomain; full description included.",
    )

    def __init__(self, subdomains: list[str], client: Optional[httpx.Client] = None, timeout: float = 10.0):
        self.subdomains = subdomains
        self._client = client
        self._timeout = timeout

    def _fetch_subdomain(self, client: httpx.Client, subdomain: str) -> list[RawJobPosting]:
        try:
            data = get_json(client, BREEZY_LIST_URL.format(subdomain=subdomain), provider="breezy")
        except ProviderHTTPError as exc:
            logger.warning("breezy subdomain '%s' fetch failed: %s", subdomain, exc)
            return []
        except Exception:
            logger.warning("breezy subdomain '%s' fetch failed", subdomain, exc_info=True)
            return []

        results = []
        for item in data if isinstance(data, list) else []:
            try:
                location = item.get("location") or {}
                url = item.get("url", "") or ""
                remote_status = "remote" if location.get("is_remote") else None
                results.append(RawJobPosting(
                    provider="breezy",
                    external_job_id=str(item.get("_id") or item.get("friendly_id") or ""),
                    title=item.get("name", "") or "",
                    company=subdomain.replace("-", " ").title(),
                    company_identifier=subdomain,
                    location=location.get("name", "") or "",
                    remote_status=remote_status,
                    description=_strip_html(item.get("description", "")),
                    url=url,
                    source_url=url,
                    employment_type_raw=item.get("type", "") or "",
                    published_at=item.get("published_date"),
                    department=item.get("department"),
                    provider_metadata={"subdomain": subdomain},
                ))
            except Exception:
                logger.warning("breezy job normalize failed for subdomain '%s'", subdomain, exc_info=True)
                continue
        return results

    def fetch_jobs(self, max_jobs: int) -> list[RawJobPosting]:
        client = self._client or build_client(self._timeout)
        owns_client = self._client is None
        try:
            results: list[RawJobPosting] = []
            for subdomain in self.subdomains:
                if len(results) >= max_jobs:
                    break
                for job in self._fetch_subdomain(client, subdomain):
                    if len(results) >= max_jobs:
                        break
                    results.append(job)
            return results
        finally:
            if owns_client:
                client.close()
