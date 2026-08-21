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

logger = logging.getLogger("providers.greenhouse")

GREENHOUSE_JOBS_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"


def _strip_html(raw_html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw_html or "")
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _display_company(token: str) -> str:
    return token.replace("-", " ").replace("_", " ").title()


def _remote_status(location: str) -> Optional[str]:
    loc = (location or "").lower()
    if "remote" in loc:
        return "remote"
    if "hybrid" in loc:
        return "hybrid"
    return None


def _normalize(token: str, item: dict) -> RawJobPosting:
    location = ((item.get("location") or {}).get("name") or "").strip()
    departments = item.get("departments") or []
    department = (departments[0].get("name") if departments else None) or None
    return RawJobPosting(
        provider="greenhouse",
        external_job_id=str(item.get("id", "")),
        title=item.get("title", "") or "",
        company=_display_company(token),
        company_identifier=token,
        location=location,
        remote_status=_remote_status(location),
        description=_strip_html(item.get("content", "")),
        url=item.get("absolute_url", "") or "",
        source_url=item.get("absolute_url", "") or "",
        published_at=item.get("updated_at"),
        department=department,
        provider_metadata={"board_token": token},
    )


class GreenhouseProvider(JobProvider):
    """Public Greenhouse job-board API (boards-api.greenhouse.io) -- no auth,
    no scraping/CAPTCHA/anti-bot bypass involved. board_tokens are the
    company-specific slug in the board's public URL."""

    name = "greenhouse"
    capabilities = ProviderCapabilities(
        provider_name="greenhouse",
        provider_version="2.0.0",
        discovery_supported=True,
        detail_fetch_supported=False,
        structured_location_supported=True,
        structured_published_at_supported=True,
        structured_salary_supported=False,
        structured_employment_type_supported=False,
        public_interface=True,
        requires_credentials=False,
        submission_supported=False,
        support_level=SupportLevel.FULL,
        notes="Single unauthenticated request per board token; no pagination needed (API returns full job list).",
    )

    def __init__(self, board_tokens: list[str], client: Optional[httpx.Client] = None, timeout: float = 10.0):
        self.board_tokens = board_tokens
        self._client = client
        self._timeout = timeout

    def _fetch_board(self, client: httpx.Client, token: str) -> list[RawJobPosting]:
        try:
            data = get_json(client, GREENHOUSE_JOBS_URL.format(token=token), provider="greenhouse",
                             params={"content": "true"})
        except ProviderHTTPError as exc:
            logger.warning("greenhouse board '%s' fetch failed: %s", token, exc)
            return []
        except Exception:
            logger.warning("greenhouse board '%s' fetch failed", token, exc_info=True)
            return []

        results = []
        for item in data.get("jobs", []):
            try:
                results.append(_normalize(token, item))
            except Exception:
                logger.warning("greenhouse job normalize failed for board '%s'", token, exc_info=True)
                continue
        return results

    def fetch_jobs(self, max_jobs: int) -> list[RawJobPosting]:
        client = self._client or build_client(self._timeout)
        owns_client = self._client is None
        try:
            tasks = [lambda t=token: self._fetch_board(client, t) for token in self.board_tokens]
            per_board = run_bounded(tasks, config.PROVIDER_CONCURRENCY_LIMIT)
            results: list[RawJobPosting] = []
            for board_jobs in per_board:
                for job in board_jobs:
                    if len(results) >= max_jobs:
                        return results
                    results.append(job)
            return results
        finally:
            if owns_client:
                client.close()
