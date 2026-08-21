"""Raw, bounded structural probes against each supported provider's own
public endpoint -- used only by the verification pipeline (app/registry/
verification.py), which needs to know whether the specific request
succeeded/failed/timed-out. This is intentionally lower-level than
JobProvider.fetch_jobs(): every concrete provider connector deliberately
swallows/logs per-tenant HTTP errors internally (so one bad board never
aborts a whole discovery cycle fetching many boards at once) -- exactly the
behavior we do NOT want here, where we need the raw outcome for exactly one
tenant. Reuses the same URL templates, User-Agent, timeout, retry, and
response-size-cap behavior as the real connectors (app.providers.http_client).

Every URL here is copied from the provider module that already implements
it -- no new endpoints, no guessed shapes."""

from typing import Callable, Optional

import httpx

from app.providers.http_client import ProviderHTTPError, build_client, request_with_retries
from app.providers.registry import workday_base_url


def _comeet_parts(tenant_identifier: str) -> tuple[str, str]:
    company, _, token = tenant_identifier.partition(":")
    return company, token


def _probe_greenhouse(client: httpx.Client, tenant: str) -> httpx.Response:
    # No content=true here -- a structural probe only needs to confirm the
    # endpoint responds and get a job count, not every job's full HTML
    # description, which can push a large tenant's response over the
    # response-size cap for no reason.
    return request_with_retries(client, "GET", f"https://boards-api.greenhouse.io/v1/boards/{tenant}/jobs",
                                 provider="greenhouse")


def _probe_lever(client: httpx.Client, tenant: str) -> httpx.Response:
    return request_with_retries(client, "GET", f"https://api.lever.co/v0/postings/{tenant}", provider="lever")


def _probe_ashby(client: httpx.Client, tenant: str) -> httpx.Response:
    return request_with_retries(client, "GET", f"https://api.ashbyhq.com/posting-api/job-board/{tenant}", provider="ashby")


def _probe_workable(client: httpx.Client, tenant: str) -> httpx.Response:
    return request_with_retries(client, "GET", f"https://apply.workable.com/api/v1/widget/accounts/{tenant}", provider="workable")


def _probe_smartrecruiters(client: httpx.Client, tenant: str) -> httpx.Response:
    return request_with_retries(client, "GET", f"https://api.smartrecruiters.com/v1/companies/{tenant}/postings", provider="smartrecruiters")


def _probe_bamboohr(client: httpx.Client, tenant: str) -> httpx.Response:
    return request_with_retries(client, "GET", f"https://{tenant}.bamboohr.com/careers/list", provider="bamboohr")


def _probe_recruitee(client: httpx.Client, tenant: str) -> httpx.Response:
    return request_with_retries(client, "GET", f"https://{tenant}.recruitee.com/api/offers/", provider="recruitee")


def _probe_breezy(client: httpx.Client, tenant: str) -> httpx.Response:
    return request_with_retries(client, "GET", f"https://{tenant}.breezy.hr/json", provider="breezy")


def _probe_comeet(client: httpx.Client, tenant: str) -> httpx.Response:
    company, token = _comeet_parts(tenant)
    return request_with_retries(client, "GET", f"https://www.comeet.com/careers-api/2.0/company/{company}/positions",
                                 provider="comeet", params={"token": token})


def _probe_workday(client: httpx.Client, tenant: str) -> httpx.Response:
    base_url = workday_base_url(tenant)
    return request_with_retries(client, "POST", base_url.rstrip("/") + "/jobs", provider="workday",
                                 json_body={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})


_PROBES: dict[str, Callable[[httpx.Client, str], httpx.Response]] = {
    "greenhouse": _probe_greenhouse,
    "lever": _probe_lever,
    "ashby": _probe_ashby,
    "workable": _probe_workable,
    "smartrecruiters": _probe_smartrecruiters,
    "bamboohr": _probe_bamboohr,
    "recruitee": _probe_recruitee,
    "breezy": _probe_breezy,
    "comeet": _probe_comeet,
    "workday": _probe_workday,
}


def has_probe(provider: str) -> bool:
    return provider.strip().lower() in _PROBES


def probe(provider: str, tenant_identifier: str, *, client: Optional[httpx.Client] = None) -> httpx.Response:
    """Raises ProviderHTTPError (or an httpx network exception) on failure --
    callers classify the failure; this function never swallows one."""
    fn = _PROBES.get(provider.strip().lower())
    if fn is None:
        raise ProviderHTTPError(provider, "no structural probe implemented for this provider")
    owns_client = client is None
    client = client or build_client()
    try:
        return fn(client, tenant_identifier)
    finally:
        if owns_client:
            client.close()
