"""Safe, bounded, public-only career-page discovery for a supplied company
domain. Given "examplecompany.com", inspects a small fixed set of normal
public paths (/, /careers, /jobs, /about/careers) plus links visibly present
on the homepage, and runs the existing ATS URL detector against them.

Hard safety bounds, per CLAUDE.md Phase 4 section 9:
  - fixed, small candidate path list -- never a general crawl
  - bounded number of pages fetched (PAGE_DISCOVERY_MAX_PAGES)
  - bounded redirects, bounded response size, bounded timeout
  - robots.txt honored (best-effort; a missing/unparseable robots.txt is
    treated as allow-all, matching standard robots.txt semantics)
  - no login pages, no CAPTCHA/anti-bot bypass, no JS execution/stealth
  - no LinkedIn/Indeed/search-engine scraping of any kind
  - descriptive User-Agent (reuses the same one as provider connectors)"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from app import config
from app.providers.detector import DetectionResult, detect_provider

logger = logging.getLogger("registry.page_discovery")

_CANDIDATE_PATHS = ["", "careers", "jobs", "about/careers", "company/careers", "about-us/careers"]
_LINK_RE = re.compile(r'href=["\']([^"\'#]+)', re.IGNORECASE)
_CAREER_WORD_RE = re.compile(r"career|job", re.IGNORECASE)


@dataclass
class DiscoveryResult:
    domain: str
    pages_fetched: int = 0
    candidate_links: list[str] = field(default_factory=list)
    best_match: Optional[DetectionResult] = None
    best_match_url: str = ""
    evidence: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _robots_allows(client: httpx.Client, base_url: str, path: str) -> bool:
    if not config.PAGE_DISCOVERY_RESPECT_ROBOTS:
        return True
    try:
        resp = client.get(urljoin(base_url, "/robots.txt"), follow_redirects=True)
        if resp.status_code >= 400:
            return True  # no robots.txt -> allow, per standard semantics
        parser = RobotFileParser()
        parser.parse(resp.text.splitlines())
        return parser.can_fetch(config.PROVIDER_USER_AGENT, urljoin(base_url, path))
    except Exception:
        return True  # fail-open on robots.txt fetch errors, never fail-closed into a hang


def _fetch_bounded(client: httpx.Client, url: str) -> Optional[httpx.Response]:
    try:
        resp = client.get(url, follow_redirects=True)
    except (httpx.TimeoutException, httpx.TransportError):
        return None
    if resp.status_code >= 400:
        return None
    if len(resp.content) > config.PAGE_DISCOVERY_MAX_RESPONSE_BYTES:
        return None
    return resp


def _registrable_family(host: str) -> str:
    """Last two DNS labels, e.g. 'about.gitlab.com' -> 'gitlab.com'. Good
    enough (not a full public-suffix-list lookup) for the narrow purpose of
    telling "still the same company's site" apart from an unrelated host."""
    labels = host.split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def _harvest_links(resp: httpx.Response, base_domain_family: str) -> tuple[list[str], list[str]]:
    """Returns (direct_ats_candidates, same-company follow-up page links) from
    one page's anchors. A same-domain link only counts as a follow-up
    candidate when it carries a career/job hint -- otherwise it's just noise
    (nav/footer/legal links), and we never want an unbounded fan-out."""
    direct, followups = [], []
    for href in _LINK_RE.findall(resp.text)[:500]:
        absolute = urljoin(str(resp.url), href)
        host = urlparse(absolute).netloc.lower()
        if not host:
            continue
        if _registrable_family(host) == base_domain_family:
            if _CAREER_WORD_RE.search(href):
                followups.append(absolute)
            continue
        direct.append(absolute)  # off-company-domain link -- treat as a direct ATS candidate
    return direct, followups


def discover_career_links(
    domain: str, *, client: Optional[httpx.Client] = None,
) -> DiscoveryResult:
    result = DiscoveryResult(domain=domain)
    base_url = f"https://{domain}" if "://" not in domain else domain
    base_domain_family = _registrable_family(domain.split("://")[-1].split("/")[0].lower())
    owns_client = client is None
    client = client or httpx.Client(
        timeout=config.PAGE_DISCOVERY_TIMEOUT_SECONDS,
        headers={"User-Agent": config.PROVIDER_USER_AGENT},
        max_redirects=config.PAGE_DISCOVERY_MAX_REDIRECTS,
    )

    try:
        seen_urls: set[str] = set()
        candidate_urls: list[str] = []
        followup_urls: list[str] = []

        def _visit(url: str) -> None:
            if result.pages_fetched >= config.PAGE_DISCOVERY_MAX_PAGES or url in seen_urls:
                return
            seen_urls.add(url)
            path = "/" + urlparse(url).path.lstrip("/")
            if not _robots_allows(client, base_url, path):
                result.evidence.append(f"skipped '{url}' -- disallowed by robots.txt")
                return
            resp = _fetch_bounded(client, url)
            result.pages_fetched += 1
            if resp is None:
                return

            detection = detect_provider(str(resp.url))
            if detection.provider and detection.tenant_identifier:
                candidate_urls.append(str(resp.url))

            direct, followups = _harvest_links(resp, base_domain_family)
            candidate_urls.extend(direct)
            followup_urls.extend(followups)

        for path in _CANDIDATE_PATHS:
            _visit(urljoin(base_url + "/", path))

        # Second hop, still inside the same page budget: a same-company page
        # explicitly linked as a careers/jobs page (e.g. a homepage link to
        # "about.example.com/jobs/") may itself embed the real ATS link.
        for url in followup_urls:
            if result.pages_fetched >= config.PAGE_DISCOVERY_MAX_PAGES:
                break
            _visit(url)

        result.candidate_links = candidate_urls[:50]

        best: Optional[DetectionResult] = None
        best_url = ""
        for link in result.candidate_links:
            detection = detect_provider(link)
            if detection.provider and (best is None or detection.confidence > best.confidence):
                best, best_url = detection, link
        result.best_match = best
        result.best_match_url = best_url
        if best:
            result.evidence.append(f"best match: {best.evidence} (url={best_url})")
        else:
            result.evidence.append("no known ATS pattern matched any discovered link")

    finally:
        if owns_client:
            client.close()

    return result
