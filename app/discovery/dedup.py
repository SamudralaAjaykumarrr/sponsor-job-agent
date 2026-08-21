import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Query params that are pure tracking/referral noise and never identify the
# actual job -- stripped so the same requisition with different campaign tags
# still canonicalizes to one URL. Deliberately NOT stripping params like
# `gh_jid`, `lever-id`, `jobId`, etc. that some ATSes use to identify the job.
TRACKING_PARAM_PREFIXES = ("utm_",)
TRACKING_PARAM_NAMES = {
    "ref", "referrer", "source", "fbclid", "gclid", "gh_src", "lever-source",
    "trk", "mc_cid", "mc_eid", "igshid", "_hsenc", "_hsmi", "src",
}


def normalize_str(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[^a-z0-9\s]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s


def canonicalize_url(url: str) -> str:
    """Normalizes a job URL for cross-provider/cross-source dedup: lowercases
    the host, strips known tracking params (preserving job-identifying ones),
    and drops a trailing slash. Returns "" for empty/unparseable input."""
    if not url:
        return ""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return ""
    if not parsed.scheme or not parsed.netloc:
        return ""

    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/") or ""

    kept_params = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAM_NAMES and not k.lower().startswith(TRACKING_PARAM_PREFIXES)
    ]
    kept_params.sort()
    query = urlencode(kept_params)

    return urlunparse((parsed.scheme.lower(), host, path, "", query, ""))


def fingerprint(company: str, title: str, location: str) -> str:
    """Cross-provider dedup fingerprint. Stable IDs (provider + external_job_id)
    and canonical URLs are checked first; this fingerprint is the last-resort
    fallback that catches the same role posted to multiple sources when
    neither of those match. NOTE: title/company/location alone is a weak
    signal -- never use it to merge jobs that already have differing stable
    provider IDs AND differing canonical URLs from callers that know better."""
    key = "|".join([normalize_str(company), normalize_str(title), normalize_str(location)])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
