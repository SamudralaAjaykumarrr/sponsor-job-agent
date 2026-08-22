import logging
import re
from datetime import datetime, timezone
from html import unescape
from typing import Optional

import httpx

from app import config
from app.providers.base import JobProvider, RawJobPosting
from app.providers.capabilities import ProviderCapabilities, SupportLevel
from app.providers.concurrency import run_bounded
from app.providers.http_client import ProviderHTTPError, build_client, get_json

logger = logging.getLogger("providers.lever")

LEVER_POSTINGS_URL = "https://api.lever.co/v0/postings/{company}"


def _strip_html(raw_html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw_html or "")
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _epoch_ms_to_iso(value) -> Optional[str]:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return None


def _remote_status(categories: dict, location: str) -> Optional[str]:
    workplace = (categories.get("workplaceType") or "").lower()
    if workplace in ("remote", "hybrid", "on-site", "onsite"):
        return "onsite" if workplace in ("on-site", "onsite") else workplace
    loc = (location or "").lower()
    if "remote" in loc:
        return "remote"
    if "hybrid" in loc:
        return "hybrid"
    return None


def _normalize(slug: str, item: dict) -> RawJobPosting:
    categories = item.get("categories") or {}
    description = item.get("descriptionPlain") or _strip_html(item.get("description", ""))
    salary = item.get("salaryRange") or {}
    location = categories.get("location", "") or ""
    return RawJobPosting(
        provider="lever",
        external_job_id=str(item.get("id", "")),
        title=item.get("text", "") or "",
        company=slug.replace("-", " ").replace("_", " ").title(),
        company_identifier=slug,
        location=location,
        remote_status=_remote_status(categories, location),
        description=description,
        url=item.get("hostedUrl", "") or "",
        source_url=item.get("hostedUrl", "") or "",
        employment_type_raw=categories.get("commitment", "") or "",
        published_at=_epoch_ms_to_iso(item.get("createdAt")),
        salary_min=salary.get("min"),
        salary_max=salary.get("max"),
        salary_currency=salary.get("currency"),
        department=categories.get("department"),
        team=categories.get("team"),
        provider_metadata={"company_slug": slug},
    )


class LeverProvider(JobProvider):
    """Public Lever postings API (api.lever.co/v0/postings/<company>) -- no
    auth, no scraping/CAPTCHA/anti-bot bypass involved."""

    name = "lever"
    capabilities = ProviderCapabilities(
        provider_name="lever",
        provider_version="2.0.0",
        discovery_supported=True,
        detail_fetch_supported=False,
        structured_location_supported=True,
        structured_published_at_supported=True,
        structured_salary_supported=True,
        structured_employment_type_supported=True,
        public_interface=True,
        requires_credentials=False,
        submission_supported=False,
        support_level=SupportLevel.FULL,
        notes="Single unauthenticated request per company slug; no pagination needed.",
    )

    def __init__(self, company_slugs: list[str], client: Optional[httpx.Client] = None, timeout: float = 10.0):
        self.company_slugs = company_slugs
        self._client = client
        self._timeout = timeout

    def _fetch_company(self, client: httpx.Client, slug: str) -> list[RawJobPosting]:
        try:
            data = get_json(client, LEVER_POSTINGS_URL.format(company=slug), provider="lever",
                             params={"mode": "json"})
        except ProviderHTTPError as exc:
            logger.warning("lever company '%s' fetch failed: %s", slug, exc)
            self._last_error = exc
            return []
        except Exception as exc:
            logger.warning("lever company '%s' fetch failed", slug, exc_info=True)
            self._last_error = exc
            return []

        results = []
        for item in data if isinstance(data, list) else []:
            try:
                results.append(_normalize(slug, item))
            except Exception:
                logger.warning("lever job normalize failed for company '%s'", slug, exc_info=True)
                continue
        return results

    def fetch_jobs(self, max_jobs: int) -> list[RawJobPosting]:
        client = self._client or build_client(self._timeout)
        owns_client = self._client is None
        try:
            tasks = [lambda s=slug: self._fetch_company(client, s) for slug in self.company_slugs]
            per_company = run_bounded(tasks, config.PROVIDER_CONCURRENCY_LIMIT)
            results: list[RawJobPosting] = []
            for jobs in per_company:
                for job in jobs:
                    if len(results) >= max_jobs:
                        return results
                    results.append(job)
            return results
        finally:
            if owns_client:
                client.close()
