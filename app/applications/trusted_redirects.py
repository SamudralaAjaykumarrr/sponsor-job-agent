"""Trusted ATS redirect model (CLAUDE.md Phase 12 sections 8-9, 26-27, 63).

Phase 11 found real employer career pages whose only apply-shaped control
pointed at a DIFFERENT host (Lever, Ashby) and classified it EXTERNAL_REDIRECT
-- correctly never clicked, but also never followed even when the destination
was obviously the employer's own recognized ATS vendor (e.g. a company career
page linking to `jobs.lever.co/<company>`). This module is the deterministic,
evidence-based answer to "is this specific external redirect actually safe to
follow": a destination host is trusted ONLY when it matches one of this
project's already-vetted, per-provider domain suffixes
(`app.applications.domain_allowlist.PROVIDER_DOMAINS`) -- never a broad "any
external link is fine" allowlist, and never a guess. Reusing that existing
table (rather than building a second, parallel one) is deliberate: it is
already the evidence this project trusts for post-navigation host checks
(`domain_allowlist.is_allowed_host_for_session`), so a pre-navigation
"should I follow this redirect at all" decision uses exactly the same
evidence, never a looser one.

Pure, dependency-free (no Playwright import) so it is unit-testable without a
browser, matching `app.applications.apply_entry`'s own design."""

from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse

from app.applications.domain_allowlist import PROVIDER_DOMAINS

# Flattened once at import time: every hostname suffix any known provider's
# application flow may legitimately end up on, plus which provider vouches
# for it. A destination matching more than one provider's suffix (this never
# actually happens with the current table, but is handled correctly) is
# still trusted -- the evidence is "this is a recognized ATS vendor domain",
# not "this is provider X specifically".
_ALL_TRUSTED_SUFFIXES: dict[str, str] = {}
for _provider, _suffixes in PROVIDER_DOMAINS.items():
    if _provider == "mock_ats":
        # CLAUDE.md Phase 8/9: the mock fixture's local/test hosts are never
        # a real redirect trust signal -- excluded so a real destination
        # host can never accidentally match "localhost"/"127.0.0.1".
        continue
    for _suffix in _suffixes:
        _ALL_TRUSTED_SUFFIXES.setdefault(_suffix, _provider)


class RedirectTrust(str, Enum):
    """CLAUDE.md Phase 12 section 8: what one apply-entry redirect
    destination evaluates to. NEVER a blanket "external is fine" -- only
    TRUSTED_ATS_REDIRECT (a recognized ATS vendor domain) or SAME_HOST (not
    actually a redirect) are ever safe to follow automatically."""
    SAME_HOST = "SAME_HOST"
    TRUSTED_ATS_REDIRECT = "TRUSTED_ATS_REDIRECT"
    UNTRUSTED = "UNTRUSTED"
    UNSAFE_SCHEME = "UNSAFE_SCHEME"


@dataclass(frozen=True)
class RedirectDecision:
    trust: RedirectTrust
    destination_host: str
    matched_provider: str = ""
    reason: str = ""


_SAFE_SCHEMES = ("http", "https", "", "file")  # "" is a relative href, resolved against current_host


def _hostname(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def classify_redirect_trust(current_host: str, href: str) -> RedirectDecision:
    """CLAUDE.md Phase 12 section 63: classifies ONE candidate destination
    (`href`, as found on the page -- relative or absolute) against the
    current page's host. Never resolves a `javascript:`/`data:`/`vbscript:`
    URL as safe (CLAUDE.md section 63's explicit unsafe-destination list) --
    those are always UNSAFE_SCHEME, regardless of visible text."""
    href = (href or "").strip()
    if not href:
        return RedirectDecision(RedirectTrust.SAME_HOST, current_host, reason="no destination -- same page")

    lowered = href.lower()
    if lowered.startswith(("javascript:", "data:", "vbscript:")):
        return RedirectDecision(RedirectTrust.UNSAFE_SCHEME, "", reason=f"unsafe URL scheme in '{href[:40]}'")

    try:
        parsed = urlparse(href)
    except ValueError:
        return RedirectDecision(RedirectTrust.UNTRUSTED, "", reason=f"malformed URL '{href[:40]}'")
    if parsed.scheme and parsed.scheme not in _SAFE_SCHEMES:
        return RedirectDecision(RedirectTrust.UNSAFE_SCHEME, "", reason=f"unsupported URL scheme '{parsed.scheme}'")

    if parsed.scheme == "file":
        # CLAUDE.md Phase 10 section 55 / app.applications.domain_allowlist's
        # own established carve-out: file:// is this project's ENTIRE local
        # test-fixture mechanism (tests/browser_fixtures.py), never a real
        # navigation target -- always trusted, exactly like
        # domain_allowlist.is_allowed_domain already treats it. A real live
        # browser test caught this: without the carve-out, every apply-entry
        # fixture's "Apply Now" link (a file:// href, since fixtures link to
        # each other on disk) was misclassified UNSAFE_SCHEME.
        return RedirectDecision(RedirectTrust.SAME_HOST, _hostname(href), reason="file:// local test fixture")

    dest_host = _hostname(href)
    if not dest_host:
        # Relative href ("/apply/123") -- resolves on the current host, so
        # this is not a cross-host redirect at all.
        return RedirectDecision(RedirectTrust.SAME_HOST, current_host, reason="relative URL, stays on current host")

    current_host = (current_host or "").lower()
    if dest_host == current_host:
        return RedirectDecision(RedirectTrust.SAME_HOST, dest_host, reason="same host as current page")

    for suffix, provider in _ALL_TRUSTED_SUFFIXES.items():
        if dest_host == suffix or dest_host.endswith("." + suffix):
            return RedirectDecision(
                RedirectTrust.TRUSTED_ATS_REDIRECT, dest_host, matched_provider=provider,
                reason=f"destination host matches known {provider} application domain '{suffix}'",
            )

    return RedirectDecision(
        RedirectTrust.UNTRUSTED, dest_host,
        reason=f"destination host '{dest_host}' does not match any recognized ATS vendor domain",
    )


class UrlProvenance(str, Enum):
    """CLAUDE.md Phase 12 sections 26-27: where a resolved application URL
    actually came from, for audit purposes. Never used to relax any safety
    check -- purely descriptive."""
    DISCOVERY_PROVIDER = "DISCOVERY_PROVIDER"
    CAREER_PORTAL = "CAREER_PORTAL"
    JOB_DETAIL = "JOB_DETAIL"
    APPLY_ENTRY = "APPLY_ENTRY"
    REDIRECT = "REDIRECT"
    USER_PROVIDED = "USER_PROVIDED"


@dataclass(frozen=True)
class ResolvedApplicationUrl:
    url: str
    provenance: UrlProvenance
    reason: str = ""


def resolve_application_url(*, canonical_url: str = "", job_url: str = "", provider: str = "") -> ResolvedApplicationUrl:
    """CLAUDE.md Phase 12 section 26: deterministic priority order for which
    URL a browser-assist session should actually open. A provider's own
    structured `canonical_url` (set by the discovery-time provider adapter --
    e.g. Greenhouse/Lever/Ashby/Workable postings already resolve directly to
    the real form) is preferred over the more generic `job.url` (often just
    the career-portal listing page, which may need an extra apply-entry hop).
    Never invents a URL that wasn't actually discovered -- an empty result
    means the caller must fall back to a manual/user-provided URL."""
    if canonical_url:
        return ResolvedApplicationUrl(
            canonical_url, UrlProvenance.DISCOVERY_PROVIDER,
            reason=f"provider '{provider}'-resolved canonical application URL" if provider else
                   "provider-resolved canonical application URL",
        )
    if job_url:
        return ResolvedApplicationUrl(job_url, UrlProvenance.JOB_DETAIL, reason="job listing/detail URL")
    return ResolvedApplicationUrl("", UrlProvenance.USER_PROVIDED, reason="no discovered URL -- user must supply one")
