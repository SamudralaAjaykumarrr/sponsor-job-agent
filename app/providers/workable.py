import logging
import re
from html import unescape
from typing import Optional

import httpx

from app import config
from app.providers.base import JobProvider, RawJobPosting
from app.providers.capabilities import ProviderCapabilities, SupportLevel
from app.providers.http_client import ProviderHTTPError, build_client, get_json

logger = logging.getLogger("providers.workable")

# Public unauthenticated widget API -- the same one Workable's embeddable
# "apply" job list widget calls from a company's own careers page.
WORKABLE_LIST_URL = "https://apply.workable.com/api/v1/widget/accounts/{account}"
# NOT api/v1/widget/accounts/{account}/jobs/{shortcode} -- live-verified
# 2026-08-22 (apply.workable.com/flosum, a real currently-listed shortcode):
# that URL 404s for every real account/shortcode, meaning per-job detail
# fetch (and therefore every real Workable job's description/requirements/
# benefits) has been silently empty this whole time -- `_fetch_detail`'s own
# `except Exception: return ""` swallowed the failure with no visible
# symptom. This v2 endpoint is the real, working replacement (found via
# Workable's own public documentation and live-confirmed to return 200 with
# description/requirements/benefits/workplace/remote/type on a real posting).
WORKABLE_DETAIL_URL = "https://apply.workable.com/api/v2/accounts/{account}/jobs/{shortcode}"

# Workable's own structured work-arrangement enum (live-verified 2026-08-22
# on the v2 detail endpoint's `workplace` field, e.g. "remote" on several
# real flosum postings; "hybrid"/"onsite" are Workable's own documented
# values for this field but were not observed live this session -- only
# ever mapped here if the value is exactly one of these three, never
# guessed for an unrecognized value).
_WORKPLACE_TO_REMOTE_STATUS = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}


def _strip_html(raw_html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw_html or "")
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _remote_status(item: dict, detail_workplace: str = "") -> Optional[str]:
    # `workplace` (from the per-job detail endpoint) is Workable's own
    # explicit work-arrangement field -- preferred first since it can
    # genuinely distinguish hybrid/onsite, which the list-endpoint booleans
    # below cannot. Only trusted when it's exactly one of the three known
    # values; an empty/unrecognized value falls through to the list-level
    # signals rather than being guessed.
    mapped = _WORKPLACE_TO_REMOTE_STATUS.get((detail_workplace or "").strip().lower())
    if mapped:
        return mapped
    # Live-verified against the real public widget API (2026-08-22,
    # apply.workable.com/api/v1/widget/accounts/flosum): the boolean field is
    # named `telecommuting`, not `telecommute` -- the prior key never matched
    # any real job, so structured remote detection silently never fired and
    # every job fell through to the text-based `city` fallback below. Both
    # keys are checked now (never fabricated, just defensive against a
    # differently-shaped tenant).
    if item.get("telecommuting") is True or item.get("telecommute") is True:
        return "remote"
    city = (item.get("city") or "").lower()
    if "remote" in city:
        return "remote"
    return None


class WorkableProvider(JobProvider):
    """Public Workable widget API (apply.workable.com/api/v1/widget/accounts/<account>)
    -- unauthenticated. Paginates via `page`; bounded by
    MAX_PAGES_PER_PROVIDER/MAX_JOBS_PER_PROVIDER. Fetches the per-job detail
    endpoint (also public/unauthenticated) for the full description, bounded
    by max_jobs so total requests stay predictable."""

    name = "workable"
    capabilities = ProviderCapabilities(
        provider_name="workable",
        provider_version="1.2.0",
        discovery_supported=True,
        detail_fetch_supported=True,
        structured_location_supported=True,
        structured_published_at_supported=True,
        structured_salary_supported=False,
        structured_employment_type_supported=True,
        public_interface=True,
        requires_credentials=False,
        submission_supported=False,
        support_level=SupportLevel.FULL,
        notes=(
            "List endpoint paginated; description requires one detail request per job (bounded by max_jobs), "
            "now against the v2 accounts/{account}/jobs/{shortcode} endpoint -- the previously-used v1/widget "
            "detail URL 404s for every real account/shortcode (live-verified 2026-08-22), which silently left "
            "description empty for every real job this whole time; fixed to the working v2 shape. "
            "remote_status prefers the v2 detail endpoint's own `workplace` enum (remote/hybrid/onsite) when "
            "recognized, else the live-verified `telecommuting` boolean (a prior version incorrectly checked a "
            "`telecommute` key that never matches any real job); url prefers `application_url` (the direct "
            "apply form) over the listing-page `url`/`shortlink`, live-verified 2026-08-22."
        ),
    )

    def __init__(self, account_subdomains: list[str], client: Optional[httpx.Client] = None, timeout: float = 10.0):
        self.account_subdomains = account_subdomains
        self._client = client
        self._timeout = timeout

    def _fetch_list_page(self, client: httpx.Client, account: str, page: int) -> Optional[dict]:
        try:
            return get_json(client, WORKABLE_LIST_URL.format(account=account), provider="workable",
                             params={"page": page})
        except ProviderHTTPError as exc:
            logger.warning("workable account '%s' page %s fetch failed: %s", account, page, exc)
            self._last_error = exc
            return None
        except Exception as exc:
            logger.warning("workable account '%s' page %s fetch failed", account, page, exc_info=True)
            self._last_error = exc
            return None

    def _fetch_detail(self, client: httpx.Client, account: str, shortcode: str) -> dict:
        """Returns {"description", "workplace"} from the v2 per-job detail
        endpoint. `description`/`requirements`/`benefits` are flat top-level
        fields on this endpoint (unlike the old, broken v1/widget detail URL
        this replaced) -- live-verified 2026-08-22. `workplace` (e.g.
        "remote") is likewise a genuine top-level field, surfaced for
        _remote_status() to prefer over the coarser list-level booleans."""
        if not shortcode:
            return {"description": "", "workplace": ""}
        try:
            detail = get_json(client, WORKABLE_DETAIL_URL.format(account=account, shortcode=shortcode),
                               provider="workable")
        except Exception:
            logger.warning("workable detail fetch failed for %s/%s", account, shortcode, exc_info=True)
            return {"description": "", "workplace": ""}
        parts = [detail.get("description", ""), detail.get("requirements", ""), detail.get("benefits", "")]
        return {
            "description": _strip_html(" ".join(p for p in parts if p)),
            "workplace": detail.get("workplace", "") or "",
        }

    def _fetch_account(self, client: httpx.Client, account: str, max_jobs: int) -> list[RawJobPosting]:
        raw_items: list[dict] = []
        seen_shortcodes: set[str] = set()
        for page in range(1, config.MAX_PAGES_PER_PROVIDER + 1):
            data = self._fetch_list_page(client, account, page)
            if not data:
                break
            jobs = data.get("jobs") or []
            if not jobs:
                break
            new_on_page = 0
            for item in jobs:
                shortcode = item.get("shortcode") or item.get("code") or ""
                if shortcode and shortcode in seen_shortcodes:
                    continue  # repeated page-token safety
                if shortcode:
                    seen_shortcodes.add(shortcode)
                raw_items.append(item)
                new_on_page += 1
            if new_on_page == 0:
                break  # provider re-served the same page -- stop instead of looping
            if len(raw_items) >= max(max_jobs, config.MAX_JOBS_PER_PROVIDER):
                break

        results = []
        for item in raw_items[: config.MAX_JOBS_PER_PROVIDER]:
            try:
                shortcode = item.get("shortcode") or item.get("code") or ""
                detail = self._fetch_detail(client, account, shortcode) if shortcode else {"description": "", "workplace": ""}
                # Live-verified 2026-08-22 (apply.workable.com/flosum): the
                # list item's own `application_url` points directly at the
                # apply FORM (".../apply"), while `url`/`shortlink` point at
                # the job's listing page one hop earlier -- preferring
                # application_url here removes an unnecessary apply-entry
                # click-through for browser-assist, matching what
                # app.applications.browser_capability_matrix's workable row
                # already independently observed live in the browser.
                url = (item.get("application_url") or item.get("url")
                       or (f"https://apply.workable.com/{account}/j/{shortcode}/" if shortcode else ""))
                results.append(RawJobPosting(
                    provider="workable",
                    external_job_id=shortcode or item.get("title", ""),
                    title=item.get("title", "") or "",
                    company=account.replace("-", " ").title(),
                    company_identifier=account,
                    location=", ".join(x for x in [item.get("city"), item.get("state"), item.get("country")] if x),
                    city=item.get("city"),
                    state=item.get("state"),
                    country=item.get("country"),
                    remote_status=_remote_status(item, detail["workplace"]),
                    description=detail["description"],
                    url=url,
                    source_url=url,
                    employment_type_raw=item.get("employment_type", "") or "",
                    published_at=item.get("published_on"),
                    department=item.get("department"),
                    provider_metadata={"account": account, "shortcode": shortcode},
                ))
            except Exception:
                logger.warning("workable job normalize failed for account '%s'", account, exc_info=True)
                continue
        return results

    def fetch_jobs(self, max_jobs: int) -> list[RawJobPosting]:
        client = self._client or build_client(self._timeout)
        owns_client = self._client is None
        try:
            results: list[RawJobPosting] = []
            for account in self.account_subdomains:
                if len(results) >= max_jobs:
                    break
                for job in self._fetch_account(client, account, max_jobs - len(results)):
                    if len(results) >= max_jobs:
                        break
                    results.append(job)
            return results
        finally:
            if owns_client:
                client.close()
