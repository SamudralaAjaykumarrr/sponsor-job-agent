import logging
import re
from html import unescape
from typing import Optional

import httpx

from app import config
from app.providers.base import JobProvider, RawJobPosting
from app.providers.capabilities import ProviderCapabilities, SupportLevel
from app.providers.concurrency import run_bounded
from app.providers.http_client import ProviderHTTPError, build_client, get_json

logger = logging.getLogger("providers.ashby")

# Ashby's public, unauthenticated job-board API -- the same endpoint the
# embeddable public job board widget calls.
ASHBY_JOB_BOARD_URL = "https://api.ashbyhq.com/posting-api/job-board/{board_name}"


def _strip_html(raw_html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw_html or "")
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _remote_status(item: dict) -> Optional[str]:
    if item.get("isRemote") is True:
        return "remote"
    location = (item.get("location") or "").lower()
    if "remote" in location:
        return "remote"
    if "hybrid" in location:
        return "hybrid"
    return None


def _normalize(board_name: str, company: str, item: dict) -> RawJobPosting:
    address = (item.get("address") or {}).get("postalAddress") or {}
    salary_range = item.get("compensation") or {}
    description = item.get("descriptionPlain") or _strip_html(item.get("descriptionHtml", ""))
    apply_url = item.get("applyUrl") or item.get("jobUrl") or ""
    return RawJobPosting(
        provider="ashby",
        external_job_id=str(item.get("id", "")),
        title=item.get("title", "") or "",
        company=company,
        company_identifier=board_name,
        location=item.get("location", "") or "",
        city=address.get("addressLocality"),
        state=address.get("addressRegion"),
        country=address.get("addressCountry"),
        remote_status=_remote_status(item),
        description=description,
        url=apply_url,
        source_url=item.get("jobUrl") or apply_url,
        employment_type_raw=item.get("employmentType", "") or "",
        published_at=item.get("publishedAt"),
        department=item.get("department"),
        team=item.get("team"),
        salary_min=(salary_range.get("summaryComponents") or [{}])[0].get("minValue")
        if salary_range.get("summaryComponents") else None,
        provider_metadata={"job_board_name": board_name},
    )


class AshbyProvider(JobProvider):
    """Public Ashby Job Board API (api.ashbyhq.com/posting-api/job-board/<name>)
    -- unauthenticated, returns every open posting for the board in one
    response (no pagination on this endpoint)."""

    name = "ashby"
    capabilities = ProviderCapabilities(
        provider_name="ashby",
        provider_version="1.0.0",
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
        notes="Job-board API returns full description + structured fields in one unauthenticated call.",
    )

    def __init__(self, job_board_names: list[str], client: Optional[httpx.Client] = None, timeout: float = 10.0):
        self.job_board_names = job_board_names
        self._client = client
        self._timeout = timeout

    def _fetch_board(self, client: httpx.Client, board_name: str) -> list[RawJobPosting]:
        try:
            data = get_json(
                client, ASHBY_JOB_BOARD_URL.format(board_name=board_name), provider="ashby",
                params={"includeCompensation": "true"},
            )
        except ProviderHTTPError as exc:
            logger.warning("ashby board '%s' fetch failed: %s", board_name, exc)
            return []
        except Exception:
            logger.warning("ashby board '%s' fetch failed", board_name, exc_info=True)
            return []

        company = data.get("organizationName") or board_name.replace("-", " ").title()
        results = []
        for item in data.get("jobs", []):
            try:
                results.append(_normalize(board_name, company, item))
            except Exception:
                logger.warning("ashby job normalize failed for board '%s'", board_name, exc_info=True)
                continue
        return results

    def fetch_jobs(self, max_jobs: int) -> list[RawJobPosting]:
        client = self._client or build_client(self._timeout)
        owns_client = self._client is None
        try:
            tasks = [lambda b=board: self._fetch_board(client, b) for board in self.job_board_names]
            per_board = run_bounded(tasks, config.PROVIDER_CONCURRENCY_LIMIT)
            results: list[RawJobPosting] = []
            for jobs in per_board:
                for job in jobs:
                    if len(results) >= max_jobs:
                        return results
                    results.append(job)
            return results
        finally:
            if owns_client:
                client.close()
