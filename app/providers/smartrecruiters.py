import logging
import re
from html import unescape
from typing import Optional

import httpx

from app import config
from app.providers.base import JobProvider, RawJobPosting
from app.providers.capabilities import ProviderCapabilities, SupportLevel
from app.providers.http_client import ProviderHTTPError, build_client, get_json

logger = logging.getLogger("providers.smartrecruiters")

SMARTRECRUITERS_LIST_URL = "https://api.smartrecruiters.com/v1/companies/{company}/postings"
SMARTRECRUITERS_DETAIL_URL = "https://api.smartrecruiters.com/v1/companies/{company}/postings/{posting_id}"
PAGE_LIMIT = 100


def _strip_html(raw_html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw_html or "")
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _remote_status(location: dict) -> Optional[str]:
    # Live-verified against the real public postings API (2026-08-22, company
    # "SmartRecruiters" itself): `location.hybrid` is a genuine, separate
    # boolean alongside `location.remote` -- e.g. a real posting with
    # region="Remote" also carried `"hybrid": false` explicitly, confirming
    # both keys are populated independently, never inferred from one another.
    if location.get("remote") is True:
        return "remote"
    if location.get("hybrid") is True:
        return "hybrid"
    return None


_COMPARABLE_CURRENCIES = {"usd"}
_COMPARABLE_PERIODS = {"yearly", "year", "annual", "annually"}


def _comparable_salary(compensation: dict) -> tuple[Optional[float], Optional[float]]:
    """`app.matching.compensation.evaluate_compensation()` (used by both
    `app.pipeline` and `app.applications.eligibility` to hard-skip a job
    below MIN_SALARY_USD) compares salary_min/salary_max as bare numbers
    against a USD-ANNUAL threshold -- it has no currency/period conversion
    anywhere in the codebase (a pre-existing gap: app.providers.lever
    already sets salary_currency and nothing downstream reads it). Real
    SmartRecruiters `compensation` data is NOT always USD/annual -- a real
    CERN posting reported `{"min": 5929, "currency": "CHF", "period":
    "MONTHLY"}`, which would look like a $5,929/YEAR salary to that gate and
    wrongly hard-skip an otherwise-fine job on a pure unit mismatch, not a
    genuine low-salary signal. Rather than teach the shared compensation gate
    currency conversion (out of scope, touches shared pipeline/eligibility
    code), this provider only ever surfaces salary_min/max when the currency
    and period are confidently USD-annual-comparable -- anything else stays
    unset here (the raw compensation dict is still preserved in
    provider_metadata for future currency-aware use, never discarded)."""
    currency = (compensation.get("currency") or "").strip().lower()
    period = (compensation.get("period") or "").strip().lower()
    if currency not in _COMPARABLE_CURRENCIES or period not in _COMPARABLE_PERIODS:
        return None, None
    return compensation.get("min"), compensation.get("max")


def _canonical_posting_url(company: str, posting_id: str) -> str:
    """Fallback candidate-facing URL when a posting's own `postingUrl`/
    `applyUrl` is unavailable. This is SmartRecruiters' own canonical public
    URL shape (live-verified 2026-08-22: https://jobs.smartrecruiters.com/
    {company}/{id} returns HTTP 200 and forwards to the full slugged URL) --
    a documented redirect target, not a guessed/fabricated link."""
    return f"https://jobs.smartrecruiters.com/{company}/{posting_id}"


class SmartRecruitersProvider(JobProvider):
    """Public SmartRecruiters Posting API
    (api.smartrecruiters.com/v1/companies/<company>/postings) -- unauthenticated.
    Paginated via offset/limit; bounded by MAX_PAGES_PER_PROVIDER /
    MAX_JOBS_PER_PROVIDER. Full description requires a per-posting detail
    request, bounded by max_jobs."""

    name = "smartrecruiters"
    capabilities = ProviderCapabilities(
        provider_name="smartrecruiters",
        provider_version="1.2.0",
        discovery_supported=True,
        detail_fetch_supported=True,
        structured_location_supported=True,
        structured_published_at_supported=True,
        structured_salary_supported=True,
        structured_employment_type_supported=True,
        public_interface=True,
        requires_credentials=False,
        submission_supported=False,
        support_level=SupportLevel.FULL,
        notes=(
            "List endpoint offset-paginated; description requires one detail request per posting. "
            "The candidate-facing URL (postingUrl/applyUrl) is live-verified (2026-08-22) to be absent "
            "from the LIST endpoint entirely -- it is read from the per-posting detail response, with a "
            "fallback to SmartRecruiters' own documented https://jobs.smartrecruiters.com/{company}/{id} "
            "redirect shape (also live-verified to resolve) if the detail fetch fails. remote_status also "
            "reads the list's separate `location.hybrid` boolean, distinct from `location.remote`. "
            "salary_min/max/currency/period come from the detail response's `compensation` object "
            "(live-verified 2026-08-22 on real CERN/NBCUniversal postings, e.g. {min:95000, max:130000, "
            "currency:USD, period:YEARLY}); present on only a minority of real postings, absent entirely "
            "on the rest -- never fabricated when missing. salary_min/max are only populated when "
            "currency/period are confidently USD-annual-comparable (app.matching.compensation's gate has "
            "no currency conversion); a non-USD/non-annual figure (e.g. CHF/MONTHLY, also live-verified) "
            "stays out of salary_min/max but is preserved verbatim in provider_metadata['raw_compensation']."
        ),
    )

    def __init__(self, company_ids: list[str], client: Optional[httpx.Client] = None, timeout: float = 10.0):
        self.company_ids = company_ids
        self._client = client
        self._timeout = timeout

    def _fetch_page(self, client: httpx.Client, company: str, offset: int) -> Optional[dict]:
        try:
            return get_json(client, SMARTRECRUITERS_LIST_URL.format(company=company), provider="smartrecruiters",
                             params={"limit": PAGE_LIMIT, "offset": offset})
        except ProviderHTTPError as exc:
            logger.warning("smartrecruiters company '%s' offset %s fetch failed: %s", company, offset, exc)
            self._last_error = exc
            return None
        except Exception as exc:
            logger.warning("smartrecruiters company '%s' offset %s fetch failed", company, offset, exc_info=True)
            self._last_error = exc
            return None

    _EMPTY_DETAIL = {
        "description": "", "posting_url": "", "active": None,
        "salary_min": None, "salary_max": None, "salary_currency": None, "salary_period": None,
        "raw_compensation": None,
    }

    def _fetch_detail(self, client: httpx.Client, company: str, posting_id: str) -> dict:
        """Returns {"description", "posting_url", "active", "salary_min",
        "salary_max", "salary_currency", "salary_period"} from the per-
        posting detail endpoint. Live-verified 2026-08-22: the LIST endpoint
        (`_fetch_page`) does NOT include `postingUrl`/`applyUrl`/`active`/
        `compensation` at all -- only this detail endpoint does.
        Description-only extraction (the old `_fetch_description`) silently
        left every real job's `url`/`source_url` empty; every caller now
        goes through this method so the candidate-facing URL is never
        dropped. `compensation` (a genuine, real, structured field -- e.g.
        `{"min": 95000, "max": 130000, "currency": "USD", "period":
        "YEARLY"}` on a real NBCUniversal posting; `{"min": 5929,
        "currency": "CHF", "period": "MONTHLY"}`, sometimes `min`-only or
        `max`-only, on real CERN postings) is present on only a minority of
        real postings observed -- absent entirely on most -- so every
        sub-field stays None rather than 0/guessed when missing.
        salary_min/salary_max are only ever populated when `_comparable_salary()`
        confirms USD/annual -- see that function's docstring for why a raw
        non-USD or non-annual figure (e.g. CHF/MONTHLY) must never reach
        those two fields. `salary_currency`/`salary_period` themselves stay
        verbatim regardless (surfaced as reported, never normalized), and
        the full raw `compensation` dict is preserved in
        `provider_metadata["raw_compensation"]` even when suppressed from
        salary_min/max, so no real data is ever discarded."""
        if not posting_id:
            return dict(self._EMPTY_DETAIL)
        try:
            detail = get_json(client, SMARTRECRUITERS_DETAIL_URL.format(company=company, posting_id=posting_id),
                               provider="smartrecruiters")
        except Exception:
            logger.warning("smartrecruiters detail fetch failed for %s/%s", company, posting_id, exc_info=True)
            return dict(self._EMPTY_DETAIL)
        sections = ((detail.get("jobAd") or {}).get("sections") or {})
        parts = [sections.get(k, {}).get("text", "") for k in ("jobDescription", "qualifications", "additionalInformation")]
        description = _strip_html(" ".join(p for p in parts if p))
        posting_url = detail.get("postingUrl") or detail.get("applyUrl") or ""
        active = detail.get("active") if isinstance(detail.get("active"), bool) else None
        compensation = detail.get("compensation") or {}
        salary_min, salary_max = _comparable_salary(compensation)
        return {
            "description": description, "posting_url": posting_url, "active": active,
            "salary_min": salary_min, "salary_max": salary_max,
            "salary_currency": compensation.get("currency") or None, "salary_period": compensation.get("period") or None,
            "raw_compensation": compensation or None,
        }

    def _fetch_company(self, client: httpx.Client, company: str, max_jobs: int) -> list[RawJobPosting]:
        postings: list[dict] = []
        offset = 0
        total_found: Optional[int] = None
        for _page in range(config.MAX_PAGES_PER_PROVIDER):
            data = self._fetch_page(client, company, offset)
            if not data:
                break
            total_found = data.get("totalFound", total_found)
            content = data.get("content") or []
            if not content:
                break
            postings.extend(content)
            offset += len(content)
            if len(postings) >= max(max_jobs, config.MAX_JOBS_PER_PROVIDER):
                break
            if total_found is not None and offset >= total_found:
                break

        results = []
        for item in postings[: config.MAX_JOBS_PER_PROVIDER]:
            try:
                posting_id = str(item.get("id", ""))
                location = item.get("location") or {}
                department = (item.get("department") or {}).get("label")
                employment = (item.get("typeOfEmployment") or {}).get("label", "") or ""
                detail = self._fetch_detail(client, company, posting_id)
                url = detail["posting_url"] or item.get("postingUrl") or _canonical_posting_url(company, posting_id)
                metadata = {"company_identifier": company, "posting_id": posting_id}
                if detail["active"] is not None:
                    metadata["active"] = detail["active"]
                if detail["raw_compensation"] is not None:
                    metadata["raw_compensation"] = detail["raw_compensation"]
                results.append(RawJobPosting(
                    provider="smartrecruiters",
                    external_job_id=posting_id,
                    title=item.get("name", "") or "",
                    company=(item.get("company") or {}).get("name") or company,
                    company_identifier=company,
                    location=", ".join(x for x in [location.get("city"), location.get("region"), location.get("country")] if x),
                    city=location.get("city"),
                    state=location.get("region"),
                    country=location.get("country"),
                    remote_status=_remote_status(location),
                    description=detail["description"],
                    url=url,
                    source_url=url,
                    employment_type_raw=employment,
                    published_at=item.get("releasedDate"),
                    department=department,
                    salary_min=detail["salary_min"],
                    salary_max=detail["salary_max"],
                    salary_currency=detail["salary_currency"],
                    salary_period=detail["salary_period"],
                    provider_metadata=metadata,
                ))
            except Exception:
                logger.warning("smartrecruiters job normalize failed for company '%s'", company, exc_info=True)
                continue
        return results

    def fetch_jobs(self, max_jobs: int) -> list[RawJobPosting]:
        client = self._client or build_client(self._timeout)
        owns_client = self._client is None
        try:
            results: list[RawJobPosting] = []
            for company in self.company_ids:
                if len(results) >= max_jobs:
                    break
                for job in self._fetch_company(client, company, max_jobs - len(results)):
                    if len(results) >= max_jobs:
                        break
                    results.append(job)
            return results
        finally:
            if owns_client:
                client.close()
