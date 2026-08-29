"""Employment Type Evidence Hardening V1: fetches a job's real, public
posting page and extracts genuine schema.org JobPosting JSON-LD structured
data -- an industry-standard SEO convention many real career-site pages
already emit -- as a THIRD independent evidence source for
app.matching.employment_type.resolve_employment_type_evidence(), alongside
the JD's own text and the provider's already-normalized structured field.

Never renders JavaScript, never authenticates, never bypasses anything (a
single bounded, unauthenticated GET through the same shared
app.providers.http_client every discovery provider already routes through).
A page that doesn't embed JSON-LD, or whose JSON-LD lacks employmentType,
simply contributes no signal -- exactly like a provider that doesn't expose
a structured field. A fetch failure here must never raise into a
feasibility/eligibility check; it degrades to "no page evidence", nothing
more."""

import json
import logging
import re
from typing import Optional

import httpx

from app.models import Job, utcnow
from app.providers.http_client import ProviderHTTPError, ResponseTooLargeError, build_client, request_with_retries

logger = logging.getLogger("applications.employment_type_evidence")

_JSONLD_BLOCK_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def extract_jobposting_employment_type(html: str) -> str:
    """Pure parse: scan HTML for <script type="application/ld+json"> blocks,
    parse each as real JSON, and return the first genuine
    JobPosting.employmentType string found. Returns "" if none exists or
    nothing parses -- never raises, never falls back to scanning ordinary
    page text (that would let arbitrary prose masquerade as structured
    metadata, which this function must never do)."""
    if not html:
        return ""
    for block in _JSONLD_BLOCK_RE.findall(html):
        try:
            data = json.loads(block.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            graph = candidate.get("@graph")
            nodes = graph if isinstance(graph, list) else [candidate]
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                node_type = node.get("@type")
                types = node_type if isinstance(node_type, list) else [node_type]
                if "JobPosting" not in types:
                    continue
                value = node.get("employmentType")
                if isinstance(value, list) and value:
                    value = value[0]
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return ""


def fetch_structured_page_employment_type(
    url: str, *, client: Optional[httpx.Client] = None, timeout: float = 10.0,
) -> str:
    """Bounded, read-only, single-request fetch of a job's real public
    posting page, returning its JobPosting JSON-LD employmentType (or "" on
    any absence/failure). Reuses the shared app.providers.http_client
    bounded-retry/response-size-cap infrastructure -- no parallel HTTP
    mechanism. A fetch failure is logged at INFO and treated as "no page
    evidence" (source=NONE upstream), never a negative signal and never
    raised into the caller."""
    if not url:
        return ""
    owns_client = client is None
    active_client = client or build_client(timeout=timeout)
    try:
        resp = request_with_retries(
            active_client, "GET", url, provider="employment-type-page-evidence", max_retries=1,
        )
        return extract_jobposting_employment_type(resp.text)
    except (ProviderHTTPError, ResponseTooLargeError) as exc:
        logger.info("employment-type page-evidence fetch failed for %s: %s", url, exc)
        return ""
    except Exception as exc:  # noqa: BLE001 -- evidence gathering must never raise into a feasibility/eligibility check
        logger.info("employment-type page-evidence fetch unexpected error for %s: %s", url, exc)
        return ""
    finally:
        if owns_client:
            active_client.close()


def refresh_page_evidence(job: Job, *, client: Optional[httpx.Client] = None) -> str:
    """Fetches page evidence for `job` (using its real, discovered
    canonical_url/url -- never a guessed URL) and PERSISTS the raw result
    (possibly "") plus a checked_at timestamp via app.jobs_repo.update_job,
    so a later resolve_employment_type_evidence() call doesn't need a fresh
    network round trip for this job. Returns the raw value found (possibly
    "" if none). Safe to call repeatedly -- always overwrites with the
    latest genuine observation, never accumulates stale duplicate state."""
    from app.jobs_repo import update_job  # local import: avoid a network-touching module being a hard import
    # dependency of app.jobs_repo's own (much more central) import graph.

    url = job.canonical_url or job.url
    raw_value = fetch_structured_page_employment_type(url, client=client)
    if job.id:
        update_job(
            job.id,
            employment_type_page_evidence_raw=raw_value,
            employment_type_page_evidence_checked_at=utcnow(),
        )
    return raw_value
