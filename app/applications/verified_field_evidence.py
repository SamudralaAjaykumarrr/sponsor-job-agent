"""Browser-Verified Answer Canonical Readiness Integration V1.

Bridges a real, live-browser-verified answer to a provider-specific
question (one with no generic candidate-profile mapping -- e.g. a
one-off employer question like "Have you ever worked for Robinhood?")
into the canonical readiness bookkeeping both execution pipelines already
use: the browser-driven `app.applications.browser_assist`/`browser_runtime`
fill pass, and the API-schema-driven `app.applications.executor.
process_execution()` pipeline (via each `ApplicationProvider.map_fields()`).

The durable record (`browser_verified_field_evidence`, see
app/migrations.py's `_m064_...`) is written ONLY by
`app.applications.browser_assist.record_verified_custom_answer()`, and
ONLY after `browser_runtime`'s own fill dispatch (`_fill_one`/
`_fill_combobox`) reports the field's ACTUAL post-selection displayed
value genuinely matches -- never merely that a click/fill call returned
without raising (CLAUDE.md Reliable Form Interaction V1: "the final
displayed/selected state is the proof", never "click succeeded").

Staleness is never a stored flag -- `is_stale()` always live-compares the
row's `job_identity_fingerprint`/`jd_fingerprint_at_verification` against
the job's CURRENT values, reusing the exact same fingerprints
`app.applications.approval.is_current_valid()` and
`app.applications.resume_integrity.verify_resume_freshness()` already use
for their own staleness checks -- no new fingerprint scheme invented. The
browser pipeline additionally re-attempts the actual fill+live-verify
against the current DOM (via the normal `_fill_one` dispatch, reusing the
SAME tested primitive that originally recorded the evidence) before ever
treating a field as resolved, so genuine per-field drift (an option
disappearing, the control changing shape) is caught even when the coarser
fingerprints still match -- this module never claims that alone is
sufficient proof of "still correct", only that it is not OBVIOUSLY stale."""

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from app.applications.models import ApplicationField, FieldCategory, FieldConfidence
from app.db import db_session
from app.models import Job

# Question-label substrings that indicate a SENSITIVE category, purely for
# accurate auditing/reporting on the synthetic ApplicationField this module
# builds -- this never gates auto-fill for THESE rows (every row here is,
# by construction, an explicit human-provided answer for this exact
# question on this exact application, which is exactly what the
# SENSITIVE_CATEGORIES/auto_fill_allowed gate exists to require -- see
# app.applications.models.SENSITIVE_CATEGORIES's own docstring).
_DEMOGRAPHIC_HINTS = ("gender", "race", "ethnicity", "disability", "veteran", "military", "pronoun", "lgbtq")
_LEGAL_HINTS = ("government official", "conflict of interest", "bribery", "personal/familial")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_question_label(label: str) -> str:
    """Same normalization family as app.applications.mapping.normalize_label
    (lowercase, collapse whitespace) -- deliberately NOT importing that
    function to avoid a mapping.py <-> this module import cycle risk;
    kept trivially identical instead."""
    lowered = (label or "").lower()
    lowered = re.sub(r"[^a-z0-9\s]", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _job_identity_fingerprint(job: Job) -> str:
    """Identical definition to app.applications.approval._job_identity_
    fingerprint -- deliberately re-implemented rather than imported, to
    avoid a circular import (approval.py may come to depend on this module
    for its own resolution reporting) and matching that module's own
    documented reason for re-implementing rather than importing
    app.applications.executor's identical helper."""
    raw = f"{job.provider}|{job.external_job_id}|{job.canonical_url or job.url}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _guess_category(question_label: str) -> FieldCategory:
    lowered = question_label.lower()
    if any(h in lowered for h in _DEMOGRAPHIC_HINTS):
        return FieldCategory.DEMOGRAPHICS
    if any(h in lowered for h in _LEGAL_HINTS):
        return FieldCategory.LEGAL_ATTESTATION
    return FieldCategory.CUSTOM_TEXT


def synthetic_field_id_for_label(question_label_normalized: str) -> str:
    """Deterministic, per-question-text id for the synthetic ApplicationField
    this module builds -- stable across calls for the SAME question text so
    a re-verification of the same question reuses the same id rather than
    silently accumulating unrelated field_ids for one real question."""
    return "verified:" + hashlib.sha1(question_label_normalized.encode("utf-8")).hexdigest()[:16]


def record_verified_answer(
    *, execution_id: str, job_id: int, provider: str, session_id: str,
    question_label: str, field_type: str, required: bool,
    expected_answer: str, actual_displayed_value: str,
    structural_form_fingerprint: str, job: Job,
) -> str:
    """The ONLY function that writes a row -- called exclusively by
    app.applications.browser_assist.record_verified_custom_answer() after a
    genuine live-DOM verification. Append-only: a re-verification of the
    same question always inserts a new row (matching this project's
    employer_sponsorship_evidence/sponsorship_decisions/human_verified_
    employment_evidence convention) rather than updating one in place."""
    now = utcnow()
    label_normalized = normalize_question_label(question_label)
    jd_fingerprint = job.resume_jd_fingerprint or job.jd_sponsorship_fingerprint or ""
    with db_session() as conn:
        conn.execute(
            """INSERT INTO browser_verified_field_evidence
               (execution_id, job_id, provider, session_id, question_label, question_label_normalized,
                field_type, required, expected_answer, actual_displayed_value,
                structural_form_fingerprint, job_identity_fingerprint, jd_fingerprint_at_verification,
                verification_result, provenance, captured_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PASS', 'browser_verified', ?, ?)""",
            (
                execution_id, job_id, provider or "", session_id or "", question_label, label_normalized,
                field_type or "", 1 if required else 0, expected_answer, actual_displayed_value,
                structural_form_fingerprint or "", _job_identity_fingerprint(job), jd_fingerprint,
                now, now,
            ),
        )
        row = conn.execute(
            "SELECT id FROM browser_verified_field_evidence WHERE execution_id = ? ORDER BY id DESC LIMIT 1",
            (execution_id,),
        ).fetchone()
    return str(row["id"]) if row else ""


def list_evidence_for_execution(execution_id: str) -> list[dict]:
    """Read-only, full history (including superseded rows) -- for audit/
    review, never used directly to decide readiness."""
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM browser_verified_field_evidence WHERE execution_id = ? ORDER BY id DESC",
            (execution_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def _latest_rows_by_label(execution_id: str) -> dict[str, dict]:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM browser_verified_field_evidence WHERE execution_id = ? ORDER BY id ASC",
            (execution_id,),
        ).fetchall()
    latest: dict[str, dict] = {}
    for r in rows:
        latest[r["question_label_normalized"]] = dict(r)  # later rows overwrite -- "latest wins"
    return latest


def is_stale(row: dict, job: Job) -> bool:
    """Live comparison only, never a stored flag -- mirrors app.applications.
    approval.is_current_valid()'s own "never cached" idiom. A mismatch on
    EITHER the job's identity fingerprint or its JD-content fingerprint
    means the posting/application this evidence was captured against is no
    longer what's currently being submitted."""
    if row.get("job_identity_fingerprint") != _job_identity_fingerprint(job):
        return True
    current_jd = job.resume_jd_fingerprint or job.jd_sponsorship_fingerprint or ""
    recorded_jd = row.get("jd_fingerprint_at_verification") or ""
    if current_jd and recorded_jd and current_jd != recorded_jd:
        return True
    return False


@dataclass
class EvidenceReconciliation:
    fields: list[ApplicationField]
    evidence_by_label: dict[str, dict]  # normalized label -> evidence row actually used


def build_application_field_overrides(execution_id: str, job: Job) -> EvidenceReconciliation:
    """Builds synthetic ApplicationField entries (one per NON-STALE, most-
    recent evidence row for this execution) that
    app.applications.mapping.match_field_with_application_fields() can
    resolve by EXACT normalized label -- never positional, never fuzzy.
    `auto_fill_allowed=True` here is correct and safe: every row this
    reads was, by construction, an explicit human-provided answer for this
    exact question (see module docstring) -- not a guess the
    SENSITIVE_CATEGORIES gate exists to block."""
    latest = _latest_rows_by_label(execution_id)
    fields: list[ApplicationField] = []
    used: dict[str, dict] = {}
    for label_normalized, row in latest.items():
        if is_stale(row, job):
            continue
        category = _guess_category(row["question_label"])
        fields.append(ApplicationField(
            field_id=synthetic_field_id_for_label(label_normalized),
            label=row["question_label"],
            category=category,
            normalized_type="select" if row.get("field_type") == "combobox" else "text",
            required=bool(row.get("required")),
            choices=[],
            value_source="browser_verified_field_evidence",
            verified_value=row["expected_answer"],
            confidence=FieldConfidence.EXACT,
            needs_user_input=False,
            sensitive=category in (FieldCategory.DEMOGRAPHICS, FieldCategory.LEGAL_ATTESTATION),
            auto_fill_allowed=True,
            reason="explicit human-provided answer, independently verified against the live "
                   "browser-rendered page for this exact question",
        ))
        used[label_normalized] = row
    return EvidenceReconciliation(fields=fields, evidence_by_label=used)
