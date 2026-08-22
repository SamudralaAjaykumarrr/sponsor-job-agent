import logging
import re
from html import unescape
from typing import Optional

import httpx

from app.providers.base import JobProvider, RawJobPosting
from app.providers.capabilities import ProviderCapabilities, SupportLevel
from app.providers.http_client import ProviderHTTPError, build_client, get_json

logger = logging.getLogger("providers.recruitee")

RECRUITEE_OFFERS_URL = "https://{subdomain}.recruitee.com/api/offers/"


def _strip_html(raw_html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw_html or "")
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _remote_status(item: dict) -> Optional[str]:
    if item.get("remote") is True:
        return "remote"
    return None


class RecruiteeProvider(JobProvider):
    """Public Recruitee offers API (<subdomain>.recruitee.com/api/offers/) --
    unauthenticated, returns every open offer (with full description) in one
    response; no pagination needed."""

    name = "recruitee"
    capabilities = ProviderCapabilities(
        provider_name="recruitee",
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
            data = get_json(client, RECRUITEE_OFFERS_URL.format(subdomain=subdomain), provider="recruitee")
        except ProviderHTTPError as exc:
            logger.warning("recruitee subdomain '%s' fetch failed: %s", subdomain, exc)
            self._last_error = exc
            return []
        except Exception as exc:
            logger.warning("recruitee subdomain '%s' fetch failed", subdomain, exc_info=True)
            self._last_error = exc
            return []

        results = []
        for item in data.get("offers", []) if isinstance(data, dict) else []:
            try:
                description = " ".join(
                    _strip_html(item.get(k, "")) for k in ("description", "requirements") if item.get(k)
                )
                url = item.get("careers_url") or ""
                results.append(RawJobPosting(
                    provider="recruitee",
                    external_job_id=str(item.get("id", "")),
                    title=item.get("title", "") or "",
                    company=subdomain.replace("-", " ").title(),
                    company_identifier=subdomain,
                    location=item.get("location", "") or "",
                    city=item.get("city"),
                    country=item.get("country"),
                    remote_status=_remote_status(item),
                    description=description,
                    url=url,
                    source_url=url,
                    employment_type_raw=item.get("employment_type", "") or "",
                    published_at=item.get("published_at") or item.get("created_at"),
                    department=item.get("department"),
                    provider_metadata={"subdomain": subdomain},
                ))
            except Exception:
                logger.warning("recruitee job normalize failed for subdomain '%s'", subdomain, exc_info=True)
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
