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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from urllib.parse import parse_qs, urlparse

from app.applications.title_normalization import titles_equivalent
from app.db import db_session

# Requisition-id shapes actually seen across this project's real providers
# (CLAUDE.md Phase 11's own live findings): Workday's "_R-1234" URL suffix
# (app.applications.workday_tenant._REQUISITION_RE), Greenhouse's numeric
# posting-id path segment, and a `?gh_jid=`/`?jobId=`/`?job=` style query
# parameter some career portals use to link to a specific listing.
_PATH_REQ_RE = re.compile(r"(?:^|[/_-])(R-?\d{3,}|\d{5,})(?:$|[/?#])")
_QUERY_KEYS = ("gh_jid", "jobid", "job_id", "job", "req", "requisition", "postingid", "posting_id")

# Lever and Ashby both identify a posting by a UUID path segment (e.g.
# jobs.lever.co/<site>/33538a2f-d27d-4a96-8f05-fa4b0e4d940e[/apply],
# jobs.ashbyhq.com/<board>/7458d4e9-da2e-47bd-98cb-adfda43d42b2[/application])
# -- never numeric or "R-"-prefixed, so `_PATH_REQ_RE` above never matches
# either provider's real id shape. Verified live against both APIs. Matched
# as a standalone `/`-delimited path segment only, never a substring of a
# longer token, so this can't accidentally fire on an unrelated hex string.
_PATH_UUID_RE = re.compile(
    r"(?:^|/)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:$|[/?#])",
    re.IGNORECASE,
)


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
    match = _PATH_UUID_RE.search(parsed.path or "")
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


# =============================================================================
# CLAUDE.md Phase 13 sections 4-10: a formal, multi-signal
# JobIdentityVerification result -- distinct from (and layered ON TOP of) the
# single-signal `verify_job_identity()` above, which stays wired into
# `app.applications.browser_runtime`'s existing per-discovery-pass gate
# exactly as it was so no already-tested SPA/apply-entry flow regresses.
# `verify_job_identity_full()` below is the richer check the executor's two
# highest-stakes moments call: immediately before a resume upload (section 9)
# and immediately before READY_FOR_FINAL_SUBMIT (section 10).
# =============================================================================

class JobIdentityVerdict(str, Enum):
    """CLAUDE.md Phase 13 section 4's exact vocabulary, corrected to this
    project's actual acceptance criteria: by DEFAULT only VERIFIED may
    continue unattended past a sensitive step (a resume upload or
    READY_FOR_FINAL_SUBMIT) -- PROBABLE/AMBIGUOUS/INSUFFICIENT all pause for
    NEEDS_USER_ACTION/review, same as MISMATCH, though MISMATCH is a
    CONFIRMED contradiction (never configurable) while the other three are
    "not enough independent evidence" (configurable via
    APPLICATION_IDENTITY_MIN_CONFIDENCE, see `_VERDICT_RANK` below)."""
    VERIFIED = "VERIFIED"
    PROBABLE = "PROBABLE"
    AMBIGUOUS = "AMBIGUOUS"
    MISMATCH = "MISMATCH"
    INSUFFICIENT = "INSUFFICIENT"


# CLAUDE.md Phase 13 acceptance correction: ordering used ONLY to interpret
# `config.APPLICATION_IDENTITY_MIN_CONFIDENCE` -- a verdict whose rank is
# BELOW the configured minimum must pause for review rather than continue
# unattended. MISMATCH is deliberately absent: it is never compared against
# this threshold, since a confirmed contradiction always pauses regardless
# of any configured confidence floor (see `meets_min_confidence` below).
_VERDICT_RANK: dict[str, int] = {
    JobIdentityVerdict.INSUFFICIENT.value: 0,
    JobIdentityVerdict.AMBIGUOUS.value: 1,
    JobIdentityVerdict.PROBABLE.value: 2,
    JobIdentityVerdict.VERIFIED.value: 3,
}


def meets_min_confidence(verdict: JobIdentityVerdict, min_confidence: str) -> bool:
    """True only when `verdict` is VERIFIED-or-above-the-configured-floor
    AND is not itself MISMATCH (a confirmed contradiction always fails this
    check, unconditionally, regardless of configuration)."""
    if verdict == JobIdentityVerdict.MISMATCH:
        return False
    min_rank = _VERDICT_RANK.get((min_confidence or "").strip().upper(), _VERDICT_RANK[JobIdentityVerdict.VERIFIED.value])
    return _VERDICT_RANK.get(verdict.value, 0) >= min_rank


@dataclass(frozen=True)
class JobIdentitySignals:
    """One side (stored -- from our own DB -- or observed -- from the live
    page) of the comparison. Every field is optional/blank when genuinely not
    known; never fabricated to fill a gap (CLAUDE.md Phase 13 section 5's
    'no candidate PII, bounded evidence' plus this project's standing 'never
    invent a field the source doesn't provide' rule)."""
    title: str = ""
    company: str = ""
    provider: str = ""
    tenant: str = ""
    site: str = ""
    url: str = ""
    requisition_id: str = ""
    location: str = ""
    employment_type: str = ""


@dataclass(frozen=True)
class JobIdentityVerification:
    verdict: JobIdentityVerdict
    signals_compared: tuple[str, ...] = field(default_factory=tuple)
    signals_matched: tuple[str, ...] = field(default_factory=tuple)
    signals_mismatched: tuple[str, ...] = field(default_factory=tuple)
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict.value, "signals_compared": list(self.signals_compared),
            "signals_matched": list(self.signals_matched), "signals_mismatched": list(self.signals_mismatched),
            "reason": self.reason,
        }


def _norm_company(name: str) -> str:
    text = (name or "").strip().lower()
    text = re.sub(r"[,.]", "", text)
    for suffix in (" inc", " llc", " ltd", " corp", " corporation", " co"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    return text.strip()


# Signals strong enough to count toward PROBABLE/VERIFIED on their own.
# `location` (CLAUDE.md section 4 explicitly names it as a signal to verify)
# is deliberately NOT here -- two genuinely different requisitions commonly
# share an identical location string ("Remote - US", a city, ...), so a
# location match alone is only ever WEAK, corroborating-only evidence
# (feeds AMBIGUOUS, never PROBABLE/VERIFIED on its own), and a location
# MISMATCH is never treated as a contradiction (a posting can legitimately
# be listed under more than one location string) -- it is simply not
# compared for mismatch purposes at all.
_STRONG_SIGNALS = frozenset({"company", "title", "provider", "tenant", "site", "requisition_id"})


def verify_job_identity_full(stored: JobIdentitySignals, observed: JobIdentitySignals) -> JobIdentityVerification:
    """CLAUDE.md Phase 13 section 4: compares every signal available on BOTH
    sides -- company, title (via title_normalization, never bare similarity),
    provider, requisition id (via `extract_requisition_token` on both URLs,
    plus an explicit `requisition_id` when the caller already parsed one
    off-URL, e.g. Workday's tenant parser), tenant/site, and location (weak,
    corroborating-only -- see `_STRONG_SIGNALS` above). Deliberately
    conservative, matching `verify_job_identity()`'s own philosophy: a
    MISMATCH is only ever returned when at least one STRONG signal that
    could be compared on both sides genuinely disagrees; a verdict is never
    inflated to VERIFIED/PROBABLE from silence, and AMBIGUOUS (some very
    weak circumstantial evidence, never enough to be PROBABLE) is distinct
    from INSUFFICIENT (nothing comparable at all)."""
    compared: list[str] = []
    matched: list[str] = []
    mismatched: list[str] = []

    def _compare(name: str, a: str, b: str, *, equal, mismatch_counts: bool = True) -> None:
        if not a or not b:
            return
        compared.append(name)
        if equal(a, b):
            matched.append(name)
        elif mismatch_counts:
            mismatched.append(name)

    _compare("company", stored.company, observed.company,
              equal=lambda a, b: _norm_company(a) == _norm_company(b))
    _compare("title", stored.title, observed.title, equal=titles_equivalent)
    _compare("provider", stored.provider, observed.provider, equal=lambda a, b: a.lower() == b.lower())
    _compare("tenant", stored.tenant, observed.tenant, equal=lambda a, b: a.lower() == b.lower())
    _compare("site", stored.site, observed.site, equal=lambda a, b: a.lower() == b.lower())

    stored_req = stored.requisition_id or extract_requisition_token(stored.url)
    observed_req = observed.requisition_id or extract_requisition_token(observed.url)
    _compare("requisition_id", stored_req, observed_req, equal=lambda a, b: a.upper() == b.upper())

    _compare("location", stored.location, observed.location,
              equal=lambda a, b: a.strip().lower() == b.strip().lower(), mismatch_counts=False)

    if not compared:
        return JobIdentityVerification(
            JobIdentityVerdict.INSUFFICIENT, reason="no comparable identity signal was available on both sides",
        )
    if mismatched:
        return JobIdentityVerification(
            JobIdentityVerdict.MISMATCH, tuple(compared), tuple(matched), tuple(mismatched),
            reason=f"signal(s) disagree: {', '.join(mismatched)}",
        )
    strong_matched = [m for m in matched if m in _STRONG_SIGNALS]
    # requisition_id is the single strongest signal this project can extract
    # (a stable, provider-issued identifier) -- matching it alone is enough
    # for VERIFIED even if other signals were unavailable to compare;
    # otherwise VERIFIED requires at least two independently-corroborating
    # STRONG signals (company+title, company+provider, etc) so a single
    # loose match (e.g. provider alone) is never over-trusted.
    if "requisition_id" in strong_matched:
        return JobIdentityVerification(
            JobIdentityVerdict.VERIFIED, tuple(compared), tuple(matched), (),
            reason="requisition id matches on both sides",
        )
    if len(strong_matched) >= 2:
        return JobIdentityVerification(
            JobIdentityVerdict.VERIFIED, tuple(compared), tuple(matched), (),
            reason=f"{len(strong_matched)} independent signals agree: {', '.join(strong_matched)}",
        )
    if len(strong_matched) == 1:
        return JobIdentityVerification(
            JobIdentityVerdict.PROBABLE, tuple(compared), tuple(matched), (),
            reason=f"only one signal available to compare and it matches: {strong_matched[0]}",
        )
    if matched:
        # Only weak signal(s) (location) matched -- some very weak
        # circumstantial evidence, never enough to be PROBABLE.
        return JobIdentityVerification(
            JobIdentityVerdict.AMBIGUOUS, tuple(compared), tuple(matched), (),
            reason=f"only weak, non-corroborating signal(s) matched: {', '.join(matched)}",
        )
    return JobIdentityVerification(
        JobIdentityVerdict.AMBIGUOUS, tuple(compared), tuple(matched), tuple(mismatched),
        reason="signals were compared but none conclusively matched or mismatched",
    )


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_verification(
    job_id: int, *, stage: str, stored: JobIdentitySignals, observed: JobIdentitySignals,
    verification: JobIdentityVerification, session_id: str = "", parser_version: str = "1",
) -> dict:
    """CLAUDE.md Phase 13 section 5: bounded identity evidence, no candidate
    PII (job-facing fields only -- title/company/provider/tenant/url/ids are
    all already-public posting metadata, never a candidate value)."""
    now = utcnow()
    with db_session() as conn:
        conn.execute(
            """INSERT INTO job_identity_verifications
               (job_id, session_id, stage, provider, provider_job_id, requisition_id,
                stored_title, observed_title, stored_company, observed_company,
                stored_url, observed_url, tenant, site, signals_compared, signals_matched,
                signals_mismatched, result, reason, parser_version, verified_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (job_id, session_id, stage, observed.provider or stored.provider, "",
             observed.requisition_id or stored.requisition_id,
             stored.title, observed.title, stored.company, observed.company,
             stored.url, observed.url, observed.tenant or stored.tenant, observed.site or stored.site,
             ",".join(verification.signals_compared), ",".join(verification.signals_matched),
             ",".join(verification.signals_mismatched), verification.verdict.value, verification.reason,
             parser_version, now),
        )
        row = conn.execute(
            "SELECT * FROM job_identity_verifications WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,),
        ).fetchone()
    return dict(row)


def list_verifications(job_id: Optional[int] = None, limit: int = 200) -> list[dict]:
    query = "SELECT * FROM job_identity_verifications"
    params: list = []
    if job_id is not None:
        query += " WHERE job_id = ?"
        params.append(job_id)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with db_session() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
