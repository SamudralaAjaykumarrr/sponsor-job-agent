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
    # Unescape BEFORE stripping tags: a real live board (caught during
    # canary prep against pumpcareers' actual `content=true` response)
    # returns its `content` field as HTML-entity-encoded markup
    # (`&lt;h3&gt;...&lt;/h3&gt;`), not literal `<h3>` tags. Stripping first
    # matched nothing (no literal `<` yet), so the entities decoded back
    # into raw, un-stripped tags afterward -- every description from every
    # tenant that encodes this way came through HTML-polluted.
    text = unescape(raw_html or "")
    text = re.sub(r"<[^>]+>", " ", text)
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


# Greenhouse's per-company custom `metadata` fields have no fixed schema --
# a company can name a field anything. We only ever READ a field whose name
# clearly signals employment type (never fabricate one); this is genuinely
# exposed structured data on the tenants that define it, honestly absent
# (employment_type_raw="") on the (majority of) tenants that don't.
_EMPLOYMENT_TYPE_METADATA_SIGNALS = ("employment type", "employment status", "job type", "worker type")


def _employment_type_from_metadata(metadata: list) -> str:
    for field in metadata or []:
        name = (field.get("name") or "").strip().lower()
        if any(signal in name for signal in _EMPLOYMENT_TYPE_METADATA_SIGNALS):
            value = field.get("value")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _normalize(token: str, item: dict) -> RawJobPosting:
    location = ((item.get("location") or {}).get("name") or "").strip()
    departments = item.get("departments") or []
    department = (departments[0].get("name") if departments else None) or None
    offices = item.get("offices") or []
    office = (offices[0].get("name") if offices else None) or None
    metadata = item.get("metadata") or []
    provider_metadata = {"board_token": token}
    requisition_id = item.get("requisition_id")
    if requisition_id:
        provider_metadata["requisition_id"] = requisition_id
    internal_job_id = item.get("internal_job_id")
    if internal_job_id:
        provider_metadata["internal_job_id"] = internal_job_id
    return RawJobPosting(
        provider="greenhouse",
        external_job_id=str(item.get("id", "")),
        title=item.get("title", "") or "",
        company=(item.get("company_name") or "").strip() or _display_company(token),
        company_identifier=token,
        location=location,
        remote_status=_remote_status(location),
        description=_strip_html(item.get("content", "")),
        url=item.get("absolute_url", "") or "",
        source_url=item.get("absolute_url", "") or "",
        employment_type_raw=_employment_type_from_metadata(metadata),
        # `first_published` is the job's genuine original publish date;
        # `updated_at` moves on every edit (a typo fix would otherwise look
        # like a brand-new posting to the freshness ranking). Only fall back
        # to `updated_at` when a tenant genuinely doesn't expose the former.
        published_at=item.get("first_published") or item.get("updated_at"),
        department=department,
        office=office,
        provider_metadata=provider_metadata,
    )


class GreenhouseProvider(JobProvider):
    """Public Greenhouse job-board API (boards-api.greenhouse.io) -- no auth,
    no scraping/CAPTCHA/anti-bot bypass involved. board_tokens are the
    company-specific slug in the board's public URL."""

    name = "greenhouse"
    capabilities = ProviderCapabilities(
        provider_name="greenhouse",
        provider_version="2.1.0",
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
        notes=(
            "Single unauthenticated request per board token; no pagination needed (API returns full job "
            "list). published_at uses the API's `first_published` (falls back to `updated_at` only when a "
            "tenant doesn't expose it) so an edit no longer looks like a brand-new posting. company_name is "
            "read directly from the API when present. employment_type_raw is a best-effort scan of the "
            "tenant's own freeform `metadata` custom fields for one named like an employment-type question "
            "(no fixed schema -- structured_employment_type_supported stays False since this is a heuristic, "
            "not a structural API guarantee like Lever/Ashby's dedicated field)."
        ),
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
            self._last_error = exc
            return []
        except Exception as exc:
            logger.warning("greenhouse board '%s' fetch failed", token, exc_info=True)
            self._last_error = exc
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
