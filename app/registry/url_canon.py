"""Canonicalization for career/ATS portal URLs -- distinct from
app.discovery.dedup.canonicalize_url (which is job-posting-URL specific).
Shares the same tracking-param stripping rules but is documented separately
because the safety requirement is stricter here: a portal URL's path/query
often carries the tenant/site/board identifier itself (Workday's
`/{tenant}/{site}` path, a `?company=` query param some boards use), so
canonicalization must never be aggressive enough to collapse two different
tenants onto the same canonical URL."""

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.discovery.dedup import TRACKING_PARAM_NAMES, TRACKING_PARAM_PREFIXES


def canonicalize_portal_url(url: str) -> str:
    """Lowercase scheme+host, strip a leading 'www.', drop fragment, strip a
    single trailing slash, strip known tracking query params (never
    tenant-identifying ones), sort remaining query params for determinism.
    Returns "" for empty/unparseable input. Preserves the full path exactly
    (minus one trailing slash) since ATS tenant/site/board identifiers
    frequently live there."""
    if not url:
        return ""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return ""
    if not parsed.scheme or not parsed.netloc:
        return ""

    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    path = parsed.path
    if path.endswith("/") and path != "/":
        path = path[:-1]

    kept_params = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAM_NAMES and not k.lower().startswith(TRACKING_PARAM_PREFIXES)
    ]
    kept_params.sort()
    query = urlencode(kept_params)

    return urlunparse((parsed.scheme.lower(), host, path, "", query, ""))


def is_valid_http_url(url: str) -> bool:
    if not url:
        return False
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)
