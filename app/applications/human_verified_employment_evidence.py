"""Human-Verified Employment Type Evidence + Canary Revalidation V1.

A FOURTH, deliberately separate employment-type evidence source alongside
the three app.matching.employment_type.resolve_employment_type_evidence()
already consults (provider structured field, JD text, JobPosting JSON-LD),
for the case those three are genuinely silent -- a real, live posting where
official ATS metadata simply omits employment type -- and a human has
independently found and verified a trustworthy external corroborating
source (e.g. a Dice/LinkedIn/company-site mirror explicitly labeling the
role "Full Time").

This module NEVER makes an employment-type decision on its own. It is
read/write storage plus one narrow consumption function
(`get_verified_value`) that returns a value ONLY when every one of these
holds simultaneously:

  1. A row exists for the exact job_id (never cross-job, never
     company-wide -- see module docstring on the migration).
  2. `identity_match_verdict == "EXACT_MATCH"` (never PROBABLE_MATCH,
     AMBIGUOUS, or MISMATCH -- those must remain UNKNOWN).
  3. `human_confirmed == 1` -- external evidence, however strong, is never
     sufficient alone; a person must have explicitly reviewed and confirmed it.
  4. The row's `posting_fingerprint_at_verification` still matches the
     job's CURRENT `jd_sponsorship_fingerprint` -- a materially changed
     posting invalidates the verification automatically, never silently
     reused against stale content.

Even when all four hold, this module's output is only ever ONE vote fed
into app.matching.employment_type.resolve_employment_type_evidence()'s
existing safer-negative-wins policy -- an explicit official CONTRACT/
PART_TIME/etc. signal from JD text or the provider's own structured field
still always overrides it. This module cannot, by itself, force a
FULL_TIME outcome against contradicting official evidence."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from app.db import db_session
from app.models import EmploymentType, Job

IDENTITY_MATCH_VERDICTS = ("EXACT_MATCH", "PROBABLE_MATCH", "AMBIGUOUS", "MISMATCH")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class HumanVerifiedEvidenceRecord:
    id: int
    job_id: int
    provider: str
    external_job_id: str
    posting_fingerprint_at_verification: str
    evidence_url: str
    evidence_source_name: str
    raw_employment_type_value: str
    normalized_value: str
    identity_match_verdict: str
    identity_match_evidence: str
    captured_at: str
    human_confirmed: bool
    human_confirmed_at: str
    human_confirmed_text: str
    created_at: str
    updated_at: str


_COLUMNS = (
    "id", "job_id", "provider", "external_job_id", "posting_fingerprint_at_verification",
    "evidence_url", "evidence_source_name", "raw_employment_type_value", "normalized_value",
    "identity_match_verdict", "identity_match_evidence", "captured_at",
    "human_confirmed", "human_confirmed_at", "human_confirmed_text", "created_at", "updated_at",
)


def _row_to_record(row) -> HumanVerifiedEvidenceRecord:
    d = dict(zip(_COLUMNS, row))
    d["human_confirmed"] = bool(d["human_confirmed"])
    return HumanVerifiedEvidenceRecord(**d)


def record_identity_check(
    job: Job, *, evidence_url: str, evidence_source_name: str, raw_employment_type_value: str,
    normalized_value: EmploymentType, identity_match_verdict: str, identity_match_evidence: str,
) -> HumanVerifiedEvidenceRecord:
    """Records an identity-match determination WITHOUT human confirmation
    (human_confirmed=0) -- the row this project's own review step produces
    before a person has actually typed the explicit confirmation phrase.
    Never itself usable by get_verified_value() until confirm_by_human()
    is called on this exact row. `job.jd_sponsorship_fingerprint` is
    snapshotted here, at IDENTITY-CHECK time, not confirmation time -- the
    two normally happen in the same short window, and snapshotting at the
    earlier point is the more conservative choice (any JD edit in between
    invalidates the pending confirmation too)."""
    if identity_match_verdict not in IDENTITY_MATCH_VERDICTS:
        raise ValueError(f"identity_match_verdict must be one of {IDENTITY_MATCH_VERDICTS}, got {identity_match_verdict!r}")
    now = utcnow()
    with db_session() as conn:
        cur = conn.execute(
            """INSERT INTO human_verified_employment_evidence (
                job_id, provider, external_job_id, posting_fingerprint_at_verification,
                evidence_url, evidence_source_name, raw_employment_type_value, normalized_value,
                identity_match_verdict, identity_match_evidence, captured_at,
                human_confirmed, human_confirmed_at, human_confirmed_text, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '', '', ?, ?)""",
            (
                job.id, job.provider or "", job.external_job_id or "",
                job.jd_sponsorship_fingerprint or "",
                evidence_url, evidence_source_name, raw_employment_type_value, normalized_value.value,
                identity_match_verdict, identity_match_evidence, now, now, now,
            ),
        )
        row_id = cur.lastrowid
        row = conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM human_verified_employment_evidence WHERE id = ?", (row_id,),
        ).fetchone()
    return _row_to_record(row)


def confirm_by_human(record_id: int, *, confirmation_text: str) -> HumanVerifiedEvidenceRecord:
    """Marks an existing identity-check row as human-confirmed. Only ever
    called after the user has typed an explicit, unambiguous confirmation
    (this project's own convention, matching the durable-approval pattern
    app.applications.approval already uses for submission authorization) --
    never inferred from a general "looks good" or "sure" style reply."""
    now = utcnow()
    with db_session() as conn:
        conn.execute(
            "UPDATE human_verified_employment_evidence SET human_confirmed = 1, "
            "human_confirmed_at = ?, human_confirmed_text = ?, updated_at = ? WHERE id = ?",
            (now, confirmation_text, now, record_id),
        )
        row = conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM human_verified_employment_evidence WHERE id = ?", (record_id,),
        ).fetchone()
    if row is None:
        raise ValueError(f"no human_verified_employment_evidence row with id={record_id}")
    return _row_to_record(row)


def get_latest_record(job_id: int) -> Optional[HumanVerifiedEvidenceRecord]:
    with db_session() as conn:
        row = conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM human_verified_employment_evidence "
            "WHERE job_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (job_id,),
        ).fetchone()
    return _row_to_record(row) if row else None


def get_verified_value(job: Job) -> Optional[EmploymentType]:
    """The ONLY function anything outside this module should call to
    consume human-verified evidence. Returns None (never a guess) unless
    ALL of: a row exists for this exact job_id, identity_match_verdict ==
    EXACT_MATCH, human_confirmed == 1, and the row's snapshotted posting
    fingerprint still matches `job.jd_sponsorship_fingerprint` right now.
    A stale/changed posting or an unconfirmed/non-exact row silently
    returns None -- exactly the "external evidence alone must never
    silently upgrade UNKNOWN" and "changed/stale posting invalidates the
    verification" rules, enforced in one place."""
    record = get_latest_record(job.id)
    if record is None:
        return None
    if record.identity_match_verdict != "EXACT_MATCH":
        return None
    if not record.human_confirmed:
        return None
    if not record.posting_fingerprint_at_verification or record.posting_fingerprint_at_verification != (job.jd_sponsorship_fingerprint or ""):
        return None
    try:
        return EmploymentType(record.normalized_value)
    except ValueError:
        return None


def resolve_for_job(job: Job) -> "EmploymentTypeDecision":
    """The one canonical, DB-only (no live network read) way to resolve a
    job's employment-type decision with human-verified evidence wired in.
    Every caller that previously called
    app.matching.employment_type.resolve_employment_type_evidence() directly
    with just the four raw-signal arguments was silently never consulting
    this module at all -- a real bug caught live: approval.py's approval
    creation AND its own freshness/staleness re-check, app/main.py's
    dashboard job-detail page, and three app.applications.doctor consistency
    checks all independently forgot the fifth `human_verified_value=`
    argument, so a genuinely human-confirmed HUMAN_VERIFIED_EXTERNAL_EVIDENCE
    record was never actually reaching any of them. Routing all of them
    through this one function instead of each repeating the same five-argument
    call closes off that whole class of "forgot to wire it in" bug at any
    future call site, not just the ones found this time.

    Deliberately excludes canary_feasibility.py's live JSON-LD page refresh
    (refresh_page_evidence) -- that caller has a genuinely different,
    already-correct contract (a live network read) that this DB-only helper
    must never silently add for callers that never expected one."""
    from app.matching.employment_type import resolve_employment_type_evidence

    return resolve_employment_type_evidence(
        job.employment_type, job.title, job.description, job.employment_type_page_evidence_raw,
        human_verified_value=get_verified_value(job),
    )
