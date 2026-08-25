"""Apply / Automation Settings V1: the single authoritative access layer for
the consumer-facing settings this feature adds -- resume optimization mode,
auto-approve-resume, cover-letter policy, submission mode (review vs.
auto-submit), the sponsorship "include likely sponsors" toggle, and job
preferences (title/company/location/work-arrangement filters).

Persisted as ONE JSON blob in the existing `app_settings` key/value table
(app/migrations.py::_m053_app_settings_table) under SETTINGS_KEY -- reusing
that table exactly as app/settings_store.py already does for its own plain
numeric knobs, rather than introducing a second, competing settings table.
The plain numeric application-limit knobs (max/day, max/company/day,
max/week, max concurrent, min salary) deliberately live in
app/settings_store.py's ALLOWED_SETTINGS instead (they map 1:1 onto existing
`config.*` attributes that are already read live elsewhere -- see that
module) -- this module only owns settings that don't already have a
`config.*` home: enums, booleans, and string lists.

Configuration precedence (CLAUDE.md Apply/Automation Settings V1 section 8):
    hard safety invariant > explicit persisted user setting > environment/default
No function in this module ever weakens a hard safety invariant (FULL_TIME
gate, sponsorship hard-skip, CAPTCHA/MFA/login boundaries, approval gating,
...) -- it only ever narrows what the ALREADY-safe pipeline auto-processes,
or mirrors a user choice onto the same `config.*` attributes the existing
safety-checked code already reads.

Submission mode is the one high-risk setting: turning it from REVIEW to
AUTO_SUBMIT requires an explicit `confirmed=True` on the SAME save call that
requests the change (see save_submission_settings) -- there is no separate
"pending" state stored server-side; an unconfirmed attempt persists nothing
and reports `needs_confirmation=True` so the caller (the /settings route)
can re-prompt. Once confirmed and saved, `config.AUTO_SUBMIT_ENABLED` is set
immediately (live) and re-applied on every process restart
(apply_overrides_on_startup) -- but ALL of the existing hard unattended
eligibility rules (app.applications.eligibility, app.applications.executor)
remain completely unmodified and unconditionally still apply; this setting
only ever supplies the "AUTO_SUBMIT_ENABLED=true" precondition those existing
gates already require, matching CLAUDE.md Phase 8's original invariant
verbatim."""

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from app import config
from app.db import db_session

SETTINGS_KEY = "apply_automation_settings_v1"

_MAX_LIST_ITEMS = 25
_MAX_ITEM_LEN = 80


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class ResumeOptimizationMode(str, Enum):
    OFF = "OFF"
    HONEST = "HONEST"
    AGGRESSIVE = "AGGRESSIVE"


class CoverLetterPolicy(str, Enum):
    OFF = "OFF"
    WHEN_REQUESTED = "WHEN_REQUESTED"
    ALWAYS = "ALWAYS"


class SubmissionMode(str, Enum):
    REVIEW = "REVIEW"
    AUTO_SUBMIT = "AUTO_SUBMIT"


WORK_ARRANGEMENTS = ("REMOTE", "HYBRID", "ONSITE")


@dataclass
class ApplySettings:
    resume_optimization_mode: str = ResumeOptimizationMode.HONEST.value
    # Default ON: this preserves this project's existing, already-tested
    # baseline behavior (a READY, one-page resume variant has always been
    # auto-promoted onto the job with no separate review gate) -- resume
    # promotion is a preparation step, not a submission, so CLAUDE.md's
    # "default should be ASSIST, not blind auto-apply" invariant is about
    # Submission mode (which DOES default to REVIEW below), not this one.
    auto_approve_resume: bool = True
    cover_letter_policy: str = CoverLetterPolicy.WHEN_REQUESTED.value
    # Default is REVIEW ("Review before submit") -- CLAUDE.md Apply/Automation
    # Settings V1 section 3 requires this be the unconditional default.
    submission_mode: str = SubmissionMode.REVIEW.value
    auto_submit_confirmed_at: str = ""
    include_likely_sponsors: bool = True
    preferred_keywords: list[str] = field(default_factory=list)
    excluded_keywords: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    work_arrangements: list[str] = field(default_factory=lambda: list(WORK_ARRANGEMENTS))
    include_companies: list[str] = field(default_factory=list)
    exclude_companies: list[str] = field(default_factory=list)
    updated_at: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class SaveResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    needs_confirmation: bool = False
    settings: Optional[ApplySettings] = None


# --- persistence -------------------------------------------------------------

def _row_to_settings(raw: dict) -> ApplySettings:
    defaults = asdict(ApplySettings())
    merged = {k: raw.get(k, v) for k, v in defaults.items()}
    return ApplySettings(**merged)


def get_settings() -> ApplySettings:
    with db_session() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (SETTINGS_KEY,)).fetchone()
    if row is None:
        return ApplySettings()
    try:
        raw = json.loads(row["value"])
    except (ValueError, TypeError):
        return ApplySettings()
    if not isinstance(raw, dict):
        return ApplySettings()
    return _row_to_settings(raw)


def _persist(settings: ApplySettings) -> None:
    settings.updated_at = utcnow()
    payload = json.dumps(asdict(settings))
    with db_session() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (SETTINGS_KEY, payload, settings.updated_at),
        )


def _apply_live_overrides(settings: ApplySettings) -> None:
    """Mirrors the two settings that have a real `config.*` home onto it --
    the exact same "setattr on the live module" mechanism
    app/settings_store.py uses -- so every existing, unmodified read site
    (app.applications.eligibility/executor for AUTO_SUBMIT_ENABLED,
    app.resume_optimizer.scheduler's SPONSORSHIP_POLICY read) sees the new
    value immediately, with no code at those sites touched."""
    config.AUTO_SUBMIT_ENABLED = settings.submission_mode == SubmissionMode.AUTO_SUBMIT.value
    config.SPONSORSHIP_POLICY = "CONFIRMED_OR_LIKELY_WITH_REVIEW" if settings.include_likely_sponsors else "CONFIRMED_ONLY"


def _save_and_apply(settings: ApplySettings) -> None:
    _persist(settings)
    _apply_live_overrides(settings)


def apply_overrides_on_startup() -> None:
    """Called once from the FastAPI lifespan, after init_db() -- re-applies
    the persisted submission-mode/sponsorship-policy choices to `config` so a
    process restart doesn't silently revert AUTO_SUBMIT_ENABLED to its .env
    default (mirrors app.settings_store.apply_overrides_on_startup)."""
    _apply_live_overrides(get_settings())


# --- validation helpers --------------------------------------------------

def _to_bool(raw) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _clean_str_list(raw, *, max_items: int = _MAX_LIST_ITEMS, max_len: int = _MAX_ITEM_LEN) -> list[str]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        items = [x.strip() for x in raw.replace("\n", ",").split(",")]
    elif isinstance(raw, (list, tuple)):
        items = [str(x).strip() for x in raw]
    else:
        items = [str(raw).strip()]
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not item:
            continue
        item = item[:max_len]
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= max_items:
            break
    return out


# --- section save functions (each independently validated/persisted) -------

def save_resume_settings(payload: dict) -> SaveResult:
    mode = str(payload.get("resume_optimization_mode") or "").strip().upper()
    if mode not in (m.value for m in ResumeOptimizationMode):
        return SaveResult(False, ["Resume optimization: choose Off, Honest, or Aggressive."])
    auto_approve = _to_bool(payload.get("auto_approve_resume"))

    settings = get_settings()
    settings.resume_optimization_mode = mode
    settings.auto_approve_resume = auto_approve
    _save_and_apply(settings)
    return SaveResult(True, settings=settings)


def save_cover_letter_settings(payload: dict) -> SaveResult:
    policy = str(payload.get("cover_letter_policy") or "").strip().upper()
    if policy not in (p.value for p in CoverLetterPolicy):
        return SaveResult(False, ["Cover letter: choose Off, When requested, or Always."])

    settings = get_settings()
    settings.cover_letter_policy = policy
    _save_and_apply(settings)
    return SaveResult(True, settings=settings)


def save_submission_settings(payload: dict, *, confirmed: bool = False) -> SaveResult:
    """See module docstring -- REVIEW -> AUTO_SUBMIT requires `confirmed=True`
    on this same call, or nothing is persisted and `needs_confirmation=True`
    is reported instead."""
    mode = str(payload.get("submission_mode") or "").strip().upper()
    if mode not in (m.value for m in SubmissionMode):
        return SaveResult(False, ["Submission: choose Review before submit or Auto-submit."])

    settings = get_settings()
    turning_on = (
        mode == SubmissionMode.AUTO_SUBMIT.value
        and settings.submission_mode != SubmissionMode.AUTO_SUBMIT.value
    )
    if turning_on and not confirmed:
        return SaveResult(False, needs_confirmation=True, settings=settings)

    if turning_on:
        settings.auto_submit_confirmed_at = utcnow()
    if mode == SubmissionMode.REVIEW.value:
        settings.auto_submit_confirmed_at = ""
    settings.submission_mode = mode
    _save_and_apply(settings)
    return SaveResult(True, settings=settings)


def save_sponsorship_settings(payload: dict) -> SaveResult:
    settings = get_settings()
    settings.include_likely_sponsors = _to_bool(payload.get("include_likely_sponsors"))
    _save_and_apply(settings)
    return SaveResult(True, settings=settings)


def save_preferences_settings(payload: dict) -> SaveResult:
    work_arrangements = [w.strip().upper() for w in _clean_str_list(payload.get("work_arrangements"), max_items=3)]
    invalid = [w for w in work_arrangements if w not in WORK_ARRANGEMENTS]
    if invalid:
        return SaveResult(False, [f"Job preferences: unknown work arrangement(s): {', '.join(invalid)}."])
    if not work_arrangements:
        # Empty selection means "no restriction" (never a silent hard-skip
        # of every job) -- matches this project's "never reject for missing
        # info" convention.
        work_arrangements = list(WORK_ARRANGEMENTS)

    settings = get_settings()
    settings.preferred_keywords = _clean_str_list(payload.get("preferred_keywords"))
    settings.excluded_keywords = _clean_str_list(payload.get("excluded_keywords"))
    settings.locations = _clean_str_list(payload.get("locations"))
    settings.work_arrangements = work_arrangements
    settings.include_companies = _clean_str_list(payload.get("include_companies"))
    settings.exclude_companies = _clean_str_list(payload.get("exclude_companies"))
    _save_and_apply(settings)
    return SaveResult(True, settings=settings)


# --- runtime consumers ------------------------------------------------------

def should_generate_cover_letter(job, settings: Optional[ApplySettings] = None) -> bool:
    """Used by app.pipeline.generate_assist_outputs. Never fabricates
    content -- only decides WHETHER to write the existing, truthful
    cover_letter.txt template."""
    s = settings or get_settings()
    if s.cover_letter_policy == CoverLetterPolicy.OFF.value:
        return False
    if s.cover_letter_policy == CoverLetterPolicy.ALWAYS.value:
        return True
    # WHEN_REQUESTED: only when the JD text itself asks for one -- a
    # meaningful, honest signal rather than a generic optional form field
    # that exists on nearly every application regardless of provider intent.
    description = (getattr(job, "description", "") or "").lower()
    return "cover letter" in description


def _text_contains_any(haystack: str, needles: list[str]) -> bool:
    haystack = haystack.lower()
    return any(n.lower() in haystack for n in needles)


def job_matches_preferences(job, settings: Optional[ApplySettings] = None) -> tuple[bool, str]:
    """Used only by app.applications.scheduler's auto-prepare candidate
    filter -- narrows WHICH already-eligible jobs get auto-prepared. Never
    touches discovery ingestion, dashboard visibility, or any manual action;
    default (empty) settings match everything, unchanged from before this
    feature existed. Never a substitute for the FULL_TIME/sponsorship hard
    gates, which this function never evaluates."""
    s = settings or get_settings()
    title = (getattr(job, "title", "") or "")
    company = (getattr(job, "company", "") or "")
    location_text = " ".join(
        str(getattr(job, attr, "") or "") for attr in ("location", "city", "state")
    )

    work_arrangement = getattr(job, "work_arrangement", None)
    wa_value = getattr(work_arrangement, "value", work_arrangement) or "UNKNOWN"
    if s.work_arrangements and wa_value != "UNKNOWN" and wa_value not in s.work_arrangements:
        return False, f"work arrangement {wa_value} not in preferred set {s.work_arrangements}"

    if s.excluded_keywords and _text_contains_any(title, s.excluded_keywords):
        return False, "title matches an excluded keyword"

    if s.preferred_keywords and not _text_contains_any(title, s.preferred_keywords):
        return False, "title does not match any preferred keyword"

    if s.exclude_companies and _text_contains_any(company, s.exclude_companies):
        return False, "company is on the excluded list"

    if s.include_companies and not _text_contains_any(company, s.include_companies):
        return False, "company is not on the included list"

    if s.locations and not _text_contains_any(location_text, s.locations):
        return False, "location does not match any preferred location"

    return True, ""
