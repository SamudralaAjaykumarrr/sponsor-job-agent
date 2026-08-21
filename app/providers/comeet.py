import logging
import re
from html import unescape
from typing import Optional

import httpx

from app.providers.base import JobProvider, RawJobPosting
from app.providers.capabilities import ProviderCapabilities, SupportLevel
from app.providers.http_client import ProviderHTTPError, build_client, get_json

logger = logging.getLogger("providers.comeet")

# Comeet's careers-api endpoint used by its own embeddable public widget.
# `token` is the public embed token shown in the company's own careers-page
# widget snippet -- not a secret/credential, but it cannot be reliably
# derived from a company name alone, so both must be configured together
# (see COMEET_COMPANY_TOKENS "company:token" format in config). Marked
# EXPERIMENTAL: the shape below is a best-effort match to the widget's known
# response and has not been verified against every tenant.
COMEET_POSITIONS_URL = "https://www.comeet.com/careers-api/2.0/company/{company}/positions"


def _strip_html(raw_html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw_html or "")
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


class CometProviderConfig:
    def __init__(self, company: str, token: str):
        self.company = company
        self.token = token


def parse_company_token_pairs(raw_pairs: list[str]) -> list[CometProviderConfig]:
    configs = []
    for pair in raw_pairs:
        if ":" not in pair:
            logger.warning("comeet config '%s' missing ':token' -- skipping", pair)
            continue
        company, token = pair.split(":", 1)
        if company and token:
            configs.append(CometProviderConfig(company.strip(), token.strip()))
    return configs


class CometProvider(JobProvider):
    """EXPERIMENTAL Comeet careers-api connector. Requires a public embed
    token per company (not obtainable from the company name alone); if not
    configured for a tenant, that tenant is silently skipped rather than
    guessed. Never claims FULL support -- unverified schema, tenant-specific
    tokens, and Comeet occasionally rotates its widget contract."""

    name = "comeet"
    capabilities = ProviderCapabilities(
        provider_name="comeet",
        provider_version="0.1.0",
        discovery_supported=True,
        detail_fetch_supported=False,
        structured_location_supported=True,
        structured_published_at_supported=False,
        structured_salary_supported=False,
        structured_employment_type_supported=False,
        public_interface=True,
        requires_credentials=False,
        submission_supported=False,
        support_level=SupportLevel.EXPERIMENTAL,
        notes="Requires a public per-company embed token; response schema is best-effort and unverified across tenants.",
    )

    def __init__(self, company_tokens: list[str], client: Optional[httpx.Client] = None, timeout: float = 10.0):
        self.configs = parse_company_token_pairs(company_tokens)
        self._client = client
        self._timeout = timeout

    def _fetch_company(self, client: httpx.Client, cfg: CometProviderConfig) -> list[RawJobPosting]:
        try:
            data = get_json(client, COMEET_POSITIONS_URL.format(company=cfg.company), provider="comeet",
                             params={"token": cfg.token})
        except ProviderHTTPError as exc:
            logger.warning("comeet company '%s' fetch failed: %s", cfg.company, exc)
            return []
        except Exception:
            logger.warning("comeet company '%s' fetch failed", cfg.company, exc_info=True)
            return []

        results = []
        items = data if isinstance(data, list) else data.get("positions", []) if isinstance(data, dict) else []
        for item in items:
            try:
                location = item.get("location") or {}
                loc_str = ", ".join(x for x in [location.get("name"), location.get("country")] if x)
                url = item.get("url_public_page") or item.get("url", "") or ""
                results.append(RawJobPosting(
                    provider="comeet",
                    external_job_id=str(item.get("uid") or item.get("id", "")),
                    title=item.get("name", "") or "",
                    company=cfg.company.replace("-", " ").title(),
                    company_identifier=cfg.company,
                    location=loc_str,
                    description=_strip_html(item.get("details", "") or item.get("description", "")),
                    url=url,
                    source_url=url,
                    department=(item.get("department") or {}).get("name") if isinstance(item.get("department"), dict) else item.get("department"),
                    provider_metadata={"company": cfg.company},
                ))
            except Exception:
                logger.warning("comeet job normalize failed for company '%s'", cfg.company, exc_info=True)
                continue
        return results

    def fetch_jobs(self, max_jobs: int) -> list[RawJobPosting]:
        client = self._client or build_client(self._timeout)
        owns_client = self._client is None
        try:
            results: list[RawJobPosting] = []
            for cfg in self.configs:
                if len(results) >= max_jobs:
                    break
                for job in self._fetch_company(client, cfg):
                    if len(results) >= max_jobs:
                        break
                    results.append(job)
            return results
        finally:
            if owns_client:
                client.close()
