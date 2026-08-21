import logging
import re
from html import unescape
from typing import Optional

import httpx

from app.providers.base import JobProvider, RawJobPosting

logger = logging.getLogger("providers.greenhouse")

GREENHOUSE_JOBS_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"


def _strip_html(raw_html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw_html or "")
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _display_company(token: str) -> str:
    return token.replace("-", " ").replace("_", " ").title()


def _normalize(token: str, item: dict) -> RawJobPosting:
    location = ((item.get("location") or {}).get("name") or "").strip()
    return RawJobPosting(
        provider="greenhouse",
        external_job_id=str(item.get("id", "")),
        title=item.get("title", "") or "",
        company=_display_company(token),
        location=location,
        description=_strip_html(item.get("content", "")),
        url=item.get("absolute_url", "") or "",
        published_at=item.get("updated_at"),
    )


class GreenhouseProvider(JobProvider):
    """Public Greenhouse job-board API (boards-api.greenhouse.io) -- no auth,
    no scraping/CAPTCHA/anti-bot bypass involved. board_tokens are the
    company-specific slug in the board's public URL."""

    name = "greenhouse"

    def __init__(self, board_tokens: list[str], client: Optional[httpx.Client] = None, timeout: float = 10.0):
        self.board_tokens = board_tokens
        self._client = client
        self._timeout = timeout

    def fetch_jobs(self, max_jobs: int) -> list[RawJobPosting]:
        results: list[RawJobPosting] = []
        client = self._client or httpx.Client(timeout=self._timeout)
        owns_client = self._client is None
        try:
            for token in self.board_tokens:
                if len(results) >= max_jobs:
                    break
                try:
                    resp = client.get(GREENHOUSE_JOBS_URL.format(token=token), params={"content": "true"})
                    resp.raise_for_status()
                    data = resp.json()
                except Exception:
                    logger.warning("greenhouse board '%s' fetch failed", token, exc_info=True)
                    continue
                for item in data.get("jobs", []):
                    if len(results) >= max_jobs:
                        break
                    try:
                        results.append(_normalize(token, item))
                    except Exception:
                        logger.warning("greenhouse job normalize failed for board '%s'", token, exc_info=True)
                        continue
        finally:
            if owns_client:
                client.close()
        return results
