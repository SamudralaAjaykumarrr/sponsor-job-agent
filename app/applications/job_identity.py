"""Job-identity verification before filling a real form (CLAUDE.md Phase 12
sections 37-39). Real ATS pages commonly show "similar jobs"/"recommended
jobs" links alongside the current posting -- an apply-entry click or
redirect must never end up filling a form for a DIFFERENT requisition than
the one this session was opened for. This module is pure, dependency-free
classification logic (no Playwright import), matching
`app.applications.apply_entry`/`app.applications.trusted_redirects`'s own
design: `app.applications.browser_runtime` supplies the real observations,
this module only judges them.

Deliberately conservative (CLAUDE.md section 38 "if mismatch: stop", never
"if in doubt: stop" applied over-eagerly): a mismatch is only ever flagged
when a requisition-shaped token can be CONFIDENTLY extracted from both the
session's original URL and the current URL and they genuinely differ. When
no token can be extracted from one or both, the result is UNVERIFIABLE, not
a guessed match or mismatch -- this project never fabricates confidence it
doesn't have."""

import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import parse_qs, urlparse

# Requisition-id shapes actually seen across this project's real providers
# (CLAUDE.md Phase 11's own live findings): Workday's "_R-1234" URL suffix
# (app.applications.workday_tenant._REQUISITION_RE), Greenhouse/Lever/
# Ashby/SmartRecruiters/Workable's numeric or opaque posting-id path
# segment, and a `?gh_jid=`/`?jobId=`/`?job=` style query parameter some
# career portals use to link to a specific listing.
_PATH_REQ_RE = re.compile(r"(?:^|[/_-])(R-?\d{3,}|\d{5,})(?:$|[/?#])")
_QUERY_KEYS = ("gh_jid", "jobid", "job_id", "job", "req", "requisition", "postingid", "posting_id")


class IdentityResult(str, Enum):
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    UNVERIFIABLE = "UNVERIFIABLE"


@dataclass(frozen=True)
class IdentityCheck:
    result: IdentityResult
    original_token: str = ""
    current_token: str = ""
    reason: str = ""


def extract_requisition_token(url: str) -> str:
    """Best-effort, conservative extraction of a requisition/posting-id-
    shaped token from a URL's path or query string. Returns "" when nothing
    confidently shaped is present -- never guesses a short/ambiguous number
    (a bare "2" or "24" is far too likely to be a page-size or pagination
    param) as a requisition id."""
    if not url:
        return ""
    parsed = urlparse(url)
    query = parse_qs(parsed.query or "")
    for key in _QUERY_KEYS:
        for actual_key, values in query.items():
            if actual_key.lower() == key and values and values[0]:
                return values[0].strip().upper()
    match = _PATH_REQ_RE.search(parsed.path or "")
    if match:
        return match.group(1).upper()
    return ""


def verify_job_identity(original_url: str, current_url: str) -> IdentityCheck:
    """CLAUDE.md Phase 12 section 38: called right before a real form is
    filled. `original_url` is the session's own recorded `application_url`
    (the job this session was opened for); `current_url` is the page the
    browser is actually on right now."""
    original_token = extract_requisition_token(original_url)
    current_token = extract_requisition_token(current_url)
    if not original_token or not current_token:
        return IdentityCheck(
            IdentityResult.UNVERIFIABLE, original_token, current_token,
            reason="no confidently-shaped requisition/posting-id token available on one or both URLs",
        )
    if original_token == current_token:
        return IdentityCheck(IdentityResult.MATCH, original_token, current_token, reason="requisition tokens match")
    return IdentityCheck(
        IdentityResult.MISMATCH, original_token, current_token,
        reason=f"original requisition token '{original_token}' does not match current page's "
               f"'{current_token}' -- possible related/recommended-job navigation",
    )
