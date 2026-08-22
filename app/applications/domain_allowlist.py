"""Provider-derived expected domains for real-browser navigation safety
(CLAUDE.md Phase 10 sections 35-36). The browser-assist layer only ever
interacts with the ATS/career domain it was told to open -- an unexpected
navigation target (ad redirect, third-party site, phishing-style bounce)
stops the session for review rather than continuing to click/fill on an
unverified page. This is deliberately a small, explicit allowlist per known
provider, never a generic "automate any site" engine."""

from urllib.parse import urlparse

# Provider name -> hostname suffixes it is legitimate for that provider's
# application flow to end up on. A URL is allowed if its hostname equals, or
# ends with "." + one of, these suffixes. Kept intentionally narrow -- add a
# provider here only once its real candidate-facing domain is confirmed.
PROVIDER_DOMAINS: dict[str, tuple[str, ...]] = {
    "greenhouse": ("greenhouse.io", "grnh.se"),
    "lever": ("lever.co",),
    "ashby": ("ashbyhq.com",),
    "workable": ("workable.com",),
    "smartrecruiters": ("smartrecruiters.com",),
    "workday": ("myworkdayjobs.com", "myworkdaysite.com", "workday.com"),
    "bamboohr": ("bamboohr.com",),
    "breezy": ("breezy.hr",),
    "recruitee": ("recruitee.com",),
    "comeet": ("comeet.co", "comeet.com"),
    "teamtailor": ("teamtailor.com",),
    "mock_ats": ("mock-ats.local", "localhost", "127.0.0.1"),
}


def _hostname(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def expected_domains(provider: str) -> tuple[str, ...]:
    return PROVIDER_DOMAINS.get((provider or "").lower(), ())


def is_allowed_domain(provider: str, url: str) -> bool:
    """True if `url`'s host is the SAME host as the job's own application URL
    (always allowed -- the page we were told to open) OR matches one of the
    provider's known domain suffixes OR is a local file:// fixture (test-only,
    never a real navigation target). Callers should pass the ORIGINAL
    application_url's host as an additional always-allowed value via
    `also_allow` when checking mid-session navigation."""
    if not url:
        return False
    scheme = urlparse(url).scheme
    if scheme == "file":
        return True
    host = _hostname(url)
    if not host:
        return False
    suffixes = expected_domains(provider)
    if not suffixes:
        # Unknown provider -- neither confirm nor deny based on a guess; the
        # caller falls back to "same host as the original application_url"
        # (see is_allowed_host_for_session below), never a broad wildcard.
        return False
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def is_allowed_host_for_session(provider: str, original_url: str, current_url: str) -> bool:
    """The actual check used mid-session: a navigation is safe if it stays on
    the exact original host (the page we were explicitly told to open, e.g. a
    Workday tenant subdomain no static list could enumerate) OR matches the
    provider's known-domain allowlist above. Anything else -- an ad network,
    an unrelated third-party host, a suspicious redirect -- is rejected.

    A `file://` URL (or any scheme with no netloc) legitimately has an EMPTY
    hostname -- checked and confirmed live against real Chromium during this
    phase's own browser E2E testing, which caught a real bug here: an
    earlier version rejected "empty hostname" outright before ever comparing
    it to the original, which meant every local test fixture was
    incorrectly treated as PLATFORM_POLICY_RESTRICTED. The correct rejection
    condition is an empty CURRENT URL (no page ever loaded), not an empty
    hostname -- an empty-vs-empty hostname comparison for two file:// URLs
    is a legitimate match."""
    if not current_url:
        return False
    original_host = _hostname(original_url)
    current_host = _hostname(current_url)
    if current_host == original_host:
        return True
    return is_allowed_domain(provider, current_url)
