import logging
from typing import Optional

import httpx

from app.providers.base import JobProvider, RawJobPosting
from app.providers.capabilities import ProviderCapabilities, SupportLevel
from app.providers.http_client import ProviderHTTPError, build_client, get_json

logger = logging.getLogger("providers.bamboohr")

BAMBOOHR_LIST_URL = "https://{subdomain}.bamboohr.com/careers/list"


class BambooHRProvider(JobProvider):
    """Public BambooHR careers list API
    (<subdomain>.bamboohr.com/careers/list) -- unauthenticated, used by the
    company's own public careers page.

    LIMITATION: this endpoint returns job metadata (title/department/location)
    but NOT the job description; BambooHR does not expose a stable public JSON
    detail endpoint for postings. Description is left empty, which correctly
    keeps sponsorship classification at UNKNOWN ("do not apply") rather than
    fabricating or guessing sponsorship from title/location alone. Marked
    PARTIAL for this reason."""

    name = "bamboohr"
    capabilities = ProviderCapabilities(
        provider_name="bamboohr",
        provider_version="1.0.0",
        discovery_supported=True,
        detail_fetch_supported=False,
        structured_location_supported=True,
        structured_published_at_supported=False,
        structured_salary_supported=False,
        structured_employment_type_supported=True,
        public_interface=True,
        requires_credentials=False,
        submission_supported=False,
        support_level=SupportLevel.PARTIAL,
        notes="No public JD detail endpoint -- description is empty, so sponsorship stays UNKNOWN by policy.",
    )

    def __init__(self, subdomains: list[str], client: Optional[httpx.Client] = None, timeout: float = 10.0):
        self.subdomains = subdomains
        self._client = client
        self._timeout = timeout

    def _fetch_subdomain(self, client: httpx.Client, subdomain: str) -> list[RawJobPosting]:
        try:
            data = get_json(client, BAMBOOHR_LIST_URL.format(subdomain=subdomain), provider="bamboohr")
        except ProviderHTTPError as exc:
            logger.warning("bamboohr subdomain '%s' fetch failed: %s", subdomain, exc)
            return []
        except Exception:
            logger.warning("bamboohr subdomain '%s' fetch failed", subdomain, exc_info=True)
            return []

        results = []
        for item in data.get("result", []) if isinstance(data, dict) else []:
            try:
                job_id = str(item.get("id", ""))
                location_label = item.get("locationLabel") or item.get("location") or ""
                url = f"https://{subdomain}.bamboohr.com/careers/{job_id}" if job_id else ""
                results.append(RawJobPosting(
                    provider="bamboohr",
                    external_job_id=job_id,
                    title=item.get("jobOpeningName", "") or "",
                    company=subdomain.replace("-", " ").title(),
                    company_identifier=subdomain,
                    location=location_label,
                    description="",
                    url=url,
                    source_url=url,
                    employment_type_raw=item.get("employmentStatusLabel", "") or "",
                    department=item.get("departmentLabel"),
                    provider_metadata={"subdomain": subdomain},
                ))
            except Exception:
                logger.warning("bamboohr job normalize failed for subdomain '%s'", subdomain, exc_info=True)
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
