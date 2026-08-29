"""Post-application recruiter/contact communication (Tsenta Remaining-Gaps
Closure V2, section 6).

The brief asks for durable product concepts covering confirmation,
recruiter/contact updates, interview/rejection updates, "Needs You" raised
from incoming communication, and history/status -- "if actual mailbox
integration already exists, integrate it cleanly; if not, do NOT invent
credentials, do NOT block the release; ensure the product has the correct
adapter/interface and a truthful 'not connected' state".

No mailbox integration exists in this codebase. This module therefore ships:

  1. `recruiter_updates` (app/migrations.py `_m060`) -- a durable history
     table any update lands in, regardless of where it came from.
  2. `MailboxAdapter` -- the interface a real future mailbox connector would
     implement. `NullMailboxAdapter` is the only implementation today, and
     it always truthfully reports `connected=False` -- it never fabricates
     credentials, never attempts a real network connection, and its
     `fetch_updates()` always returns an empty list.
  3. `classify_update_text()` -- a pure, deterministic, keyword-based
     classifier a real mailbox adapter (or a human typing in a manual
     update) can use to guess an update's TYPE. It is advisory only:
     `record_update()` never trusts it to decide anything safety-relevant
     (it cannot mark an execution APPLIED, cannot resolve a blocker, and
     cannot itself raise a Needs-You notification unless the caller
     explicitly asks it to via `raise_needs_you=True`).

This module never marks an execution APPLIED, WITHDRAWN, or any other
terminal state on its own -- app.applications.handoff.record_manual_outcome
and the genuine confirmation/receipt path remain the only ways an execution
becomes APPLIED. A recruiter update recording "I got a confirmation email"
is evidence for a HUMAN to act on via the existing handoff flow, never a
second, parallel way to fabricate a receipt."""

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from typing import Optional

from app import notifications as _notifications
from app.db import db_session

SOURCE_MANUAL = "manual"
SOURCE_MAILBOX = "mailbox"

UPDATE_CONFIRMATION = "CONFIRMATION"
UPDATE_INTERVIEW_REQUEST = "INTERVIEW_REQUEST"
UPDATE_REJECTION = "REJECTION"
UPDATE_STATUS_CHECK_IN = "STATUS_CHECK_IN"
UPDATE_OTHER = "OTHER"

VALID_UPDATE_TYPES = frozenset({
    UPDATE_CONFIRMATION, UPDATE_INTERVIEW_REQUEST, UPDATE_REJECTION, UPDATE_STATUS_CHECK_IN, UPDATE_OTHER,
})

# Ordered so the first genuine match wins -- rejection/interview language is
# checked before the more generic "status check-in" bucket, and this list is
# advisory/classification-only (see module docstring): it never decides
# anything a human hasn't asked it to.
_INTERVIEW_PATTERNS = (
    r"\binterview\b", r"\bschedule a call\b", r"\bphone screen\b", r"\bnext steps?\b.{0,20}\binterview\b",
)
_REJECTION_PATTERNS = (
    r"\bnot moving forward\b", r"\bdecided to move forward with other\b", r"\bunfortunately\b",
    r"\bwe regret\b", r"\bposition (has been|was) filled\b", r"\bwill not be (proceeding|moving forward)\b",
)
_CONFIRMATION_PATTERNS = (
    r"\bapplication (was |has been )?received\b", r"\bthank you for applying\b", r"\bwe('| ha)ve received your\b",
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def classify_update_text(subject: str, body: str = "") -> str:
    """Pure, deterministic, advisory-only classification. Never raises,
    never returns anything outside VALID_UPDATE_TYPES, and a caller must
    never treat its result as verified evidence -- it is a convenience
    default for a manual-entry form's dropdown, nothing more."""
    text = f"{subject or ''} {body or ''}".lower()
    if not text.strip():
        return UPDATE_OTHER
    for pattern in _REJECTION_PATTERNS:
        if re.search(pattern, text):
            return UPDATE_REJECTION
    for pattern in _INTERVIEW_PATTERNS:
        if re.search(pattern, text):
            return UPDATE_INTERVIEW_REQUEST
    for pattern in _CONFIRMATION_PATTERNS:
        if re.search(pattern, text):
            return UPDATE_CONFIRMATION
    return UPDATE_STATUS_CHECK_IN


@dataclass(frozen=True)
class InboundUpdate:
    """One update as a real mailbox adapter would report it -- never
    fabricated; every field defaults to blank/empty rather than guessed."""
    subject: str
    body: str = ""
    raw_reference: str = ""
    received_at: str = ""


class MailboxAdapter(ABC):
    """The interface a real future mailbox connector would implement.
    Nothing in this codebase may claim `connected=True` without a genuine,
    user-authorized, credentialed connection actually being present --
    see NullMailboxAdapter, the only implementation today."""

    @abstractmethod
    def is_connected(self) -> bool:
        ...

    @abstractmethod
    def status_detail(self) -> str:
        ...

    @abstractmethod
    def fetch_updates(self, *, since: Optional[str] = None) -> list[InboundUpdate]:
        ...


class NullMailboxAdapter(MailboxAdapter):
    """The only MailboxAdapter this project ships. Always truthfully
    reports 'not connected' -- never invents credentials, never attempts a
    network connection, never scrapes a real mailbox. A user records
    updates manually (via `record_update(source=SOURCE_MANUAL, ...)`) until
    a genuine, explicitly-authorized mailbox integration is built."""

    def is_connected(self) -> bool:
        return False

    def status_detail(self) -> str:
        return "No mailbox is connected. Record updates from recruiters or ATS emails manually below."

    def fetch_updates(self, *, since: Optional[str] = None) -> list[InboundUpdate]:
        return []


_ACTIVE_ADAPTER: MailboxAdapter = NullMailboxAdapter()


def get_mailbox_adapter() -> MailboxAdapter:
    return _ACTIVE_ADAPTER


def mailbox_status() -> dict:
    adapter = get_mailbox_adapter()
    return {"connected": adapter.is_connected(), "detail": adapter.status_detail()}


@dataclass
class RecordUpdateResult:
    ok: bool
    detail: str
    update: Optional[dict] = None


def record_update(
    job_id: int, update_type: str, *, execution_id: str = "", source: str = SOURCE_MANUAL,
    subject: str = "", detail: str = "", raw_reference: str = "", raise_needs_you: bool = False,
) -> RecordUpdateResult:
    """Records one durable recruiter/contact update. Never marks any
    execution APPLIED/terminal on its own -- see module docstring. When
    `raise_needs_you=True` (the caller's own explicit decision, e.g. an
    interview request or rejection a human should see), fires the SAME
    existing `app.applications.notifications.notify` choke point every
    other Needs-You notification already uses -- never a second, parallel
    notification mechanism."""
    if update_type not in VALID_UPDATE_TYPES:
        return RecordUpdateResult(False, f"unknown update_type '{update_type}'")
    if source not in (SOURCE_MANUAL, SOURCE_MAILBOX):
        return RecordUpdateResult(False, f"unknown source '{source}'")

    now = utcnow()
    needs_you_flag = 1 if raise_needs_you else 0
    with db_session() as conn:
        cur = conn.execute(
            """INSERT INTO recruiter_updates
               (job_id, execution_id, update_type, source, subject, detail, raw_reference, needs_you, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (job_id, execution_id or "", update_type, source, (subject or "")[:300], (detail or "")[:2000],
             (raw_reference or "")[:500], needs_you_flag, now),
        )
        row = conn.execute("SELECT * FROM recruiter_updates WHERE id = ?", (cur.lastrowid,)).fetchone()

    if raise_needs_you:
        _notifications.notify(
            _notifications.KIND_NEEDS_YOU,
            title=f"Update received: {update_type.replace('_', ' ').title()}",
            message=(subject or detail or "A new recruiter/application update needs your attention.")[:200],
            job_id=job_id, execution_id=execution_id or None,
            dedupe_key=f"recruiter_update:{job_id}:{update_type}",
        )

    return RecordUpdateResult(True, "recorded", dict(row) if row else None)


def list_updates(job_id: Optional[int] = None, *, limit: int = 50) -> list[dict]:
    query = "SELECT * FROM recruiter_updates"
    params: tuple = ()
    if job_id is not None:
        query += " WHERE job_id = ?"
        params = (job_id,)
    query += " ORDER BY id DESC LIMIT ?"
    params = params + (limit,)
    with db_session() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
