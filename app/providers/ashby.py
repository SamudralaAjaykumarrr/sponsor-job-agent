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


def _salary_period(interval: str) -> Optional[str]:
    # Ashby's own values: "1 YEAR" | "1 HOUR" | "1 MONTH" | "1 WEEK" | "NONE" | ...
    text = (interval or "").lower()
    if "hour" in text:
        return "hour"
    if "month" in text:
        return "month"
    if "week" in text:
        return "week"
    if "year" in text:
        return "year"
    return None


def _extract_salary(compensation: dict) -> tuple:
    """Ashby's `compensation.summaryComponents` is a list of components of
    DIFFERENT types (Salary/Bonus/EquityPercentage/...) in no guaranteed
    order -- picking summaryComponents[0] unconditionally (as this used to)
    silently returns whichever component happens to sort first, e.g. an
    equity/bonus component with minValue=None, even on postings that DO
    expose a real salary range. Only a component whose compensationType
    genuinely says "Salary" is ever used."""
    for component in compensation.get("summaryComponents") or []:
        if not isinstance(component, dict):
            continue
        if "salary" not in (component.get("compensationType") or "").lower():
            continue
        return (
            component.get("minValue"),
            component.get("maxValue"),
            component.get("currencyCode"),
            _salary_period(component.get("interval", "")),
        )
    return (None, None, None, None)


def _normalize(board_name: str, company: str, item: dict) -> RawJobPosting:
    address = (item.get("address") or {}).get("postalAddress") or {}
    compensation = item.get("compensation") or {}
    description = item.get("descriptionPlain") or _strip_html(item.get("descriptionHtml", ""))
    apply_url = item.get("applyUrl") or item.get("jobUrl") or ""
    salary_min, salary_max, salary_currency, salary_period = _extract_salary(compensation)
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
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency=salary_currency,
        salary_period=salary_period,
        provider_metadata={"job_board_name": board_name},
    )


class AshbyProvider(JobProvider):
    """Public Ashby Job Board API (api.ashbyhq.com/posting-api/job-board/<name>)
    -- unauthenticated, returns every open posting for the board in one
    response (no pagination on this endpoint)."""

    name = "ashby"
    capabilities = ProviderCapabilities(
        provider_name="ashby",
        provider_version="1.1.0",
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
        notes=(
            "Job-board API returns full description + structured fields in one unauthenticated call. "
            "compensation.summaryComponents holds MULTIPLE component types (Salary/Bonus/EquityPercentage) "
            "in no guaranteed order -- salary_min/max/currency/period are now taken only from the component "
            "whose compensationType is genuinely 'Salary' (previously took summaryComponents[0] "
            "unconditionally, which was frequently a non-salary component and never read maxValue at all)."
        ),
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
            self._last_error = exc
            return []
        except Exception as exc:
            logger.warning("ashby board '%s' fetch failed", board_name, exc_info=True)
            self._last_error = exc
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
