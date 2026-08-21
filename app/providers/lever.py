import logging
import re
from datetime import datetime, timezone
from html import unescape
from typing import Optional

import httpx

from app.providers.base import JobProvider, RawJobPosting

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


def _normalize(slug: str, item: dict) -> RawJobPosting:
    categories = item.get("categories") or {}
    description = item.get("descriptionPlain") or _strip_html(item.get("description", ""))
    salary = item.get("salaryRange") or {}
    return RawJobPosting(
        provider="lever",
        external_job_id=str(item.get("id", "")),
        title=item.get("text", "") or "",
        company=slug.replace("-", " ").replace("_", " ").title(),
        location=categories.get("location", "") or "",
        description=description,
        url=item.get("hostedUrl", "") or "",
        employment_type_raw=categories.get("commitment", "") or "",
        published_at=_epoch_ms_to_iso(item.get("createdAt")),
        salary_min=salary.get("min"),
        salary_max=salary.get("max"),
    )


class LeverProvider(JobProvider):
    """Public Lever postings API (api.lever.co/v0/postings/<company>) -- no
    auth, no scraping/CAPTCHA/anti-bot bypass involved."""

    name = "lever"

    def __init__(self, company_slugs: list[str], client: Optional[httpx.Client] = None, timeout: float = 10.0):
        self.company_slugs = company_slugs
        self._client = client
        self._timeout = timeout

    def fetch_jobs(self, max_jobs: int) -> list[RawJobPosting]:
        results: list[RawJobPosting] = []
        client = self._client or httpx.Client(timeout=self._timeout)
        owns_client = self._client is None
        try:
            for slug in self.company_slugs:
                if len(results) >= max_jobs:
                    break
                try:
                    resp = client.get(LEVER_POSTINGS_URL.format(company=slug), params={"mode": "json"})
                    resp.raise_for_status()
                    data = resp.json()
                except Exception:
                    logger.warning("lever company '%s' fetch failed", slug, exc_info=True)
                    continue
                for item in data if isinstance(data, list) else []:
                    if len(results) >= max_jobs:
                        break
                    try:
                        results.append(_normalize(slug, item))
                    except Exception:
                        logger.warning("lever job normalize failed for company '%s'", slug, exc_info=True)
                        continue
        finally:
            if owns_client:
                client.close()
        return results
