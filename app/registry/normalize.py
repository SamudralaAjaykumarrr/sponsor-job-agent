"""Deterministic company name / domain normalization for registry dedup.

Domain is the primary disambiguator for company identity (see store.py's
dedup key = normalized_name + primary_domain) -- normalizing the *name* alone
is only for grouping/search/display, never enough by itself to merge two
companies. Two companies with similar names but different domains must stay
distinct rows."""

import re
from urllib.parse import urlparse

# Common legal-entity suffixes, stripped repeatedly (handles "Acme Corp, Inc.").
# Deliberately conservative -- short generic words are NOT included here, so
# "Acme Co" (a real short business name) only loses "Co"/"Co." specifically,
# not any word that merely starts with those letters.
_SUFFIX_WORDS = (
    "incorporated", "inc", "llc", "l l c", "corporation", "corp",
    "limited", "ltd", "co",
)
_SUFFIX_RE = re.compile(
    r"[,]?\s+(" + "|".join(re.escape(w) for w in _SUFFIX_WORDS) + r")\.?$",
    re.IGNORECASE,
)
_PUNCT_RE = re.compile(r"[^\w\s&-]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize_company_name(name: str) -> str:
    """'Acme Widgets, Inc.' / 'ACME WIDGETS LLC' / 'acme   widgets corp.' -> 'acme widgets'.
    Does not strip generic (non-suffix) words, so distinct companies with
    different real names never collide."""
    s = (name or "").strip().lower()
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    while True:
        stripped = _SUFFIX_RE.sub("", s).strip()
        if stripped == s:
            break
        s = stripped
    return s


def normalize_domain(value: str) -> str:
    """'https://www.Acme.com/careers' / 'WWW.ACME.COM' / 'acme.com' -> 'acme.com'.
    Returns "" for empty/unparseable input. Only normalizes the host --
    callers that need the path (e.g. career portal URLs) should use
    app.registry.url_canon instead, which preserves tenant-identifying path/
    query elements."""
    s = (value or "").strip().lower()
    if not s:
        return ""
    if "://" not in s:
        s = "//" + s
    try:
        parsed = urlparse(s)
    except ValueError:
        return ""
    host = parsed.netloc or parsed.path.split("/")[0]
    host = host.split("@")[-1]  # drop any userinfo
    host = host.split(":")[0]  # drop port
    if host.startswith("www."):
        host = host[4:]
    return host.strip(".")
