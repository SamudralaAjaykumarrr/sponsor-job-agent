"""Eligibility gate BEFORE execution (CLAUDE.md Phase 8 section 2). Re-derives
the checks independently of whatever app.models.ApplicationState the
pipeline already computed -- defense in depth, matching the durable rule
"no automated application submission may occur unless the job has been
positively classified as FULL_TIME". Also implements the FULL_TIME hard gate
(section 1) as its first, unconditional check."""

import json
from dataclasses import dataclass, field

from app import config
from app.config import NEEDS_USER_INPUT
from app.matching.compensation import evaluate_compensation
from app.matching.employment_type import classify_employment_type
from app.matching.geography import is_us_location
from app.matching.roles import is_target_role
from app.matching.seniority import evaluate_seniority
from app.models import ApplicationState, EmploymentType, Job, SponsorshipStatus

_TERMINAL_SKIP_STATES = {
    ApplicationState.SKIPPED, ApplicationState.SKIPPED_NO_SPONSORSHIP,
    ApplicationState.SKIPPED_SENIORITY, ApplicationState.SKIPPED_COMPENSATION,
    ApplicationState.SKIPPED_POOR_MATCH, ApplicationState.CLAIM_VALIDATION_FAILED,
    ApplicationState.REJECTED, ApplicationState.WITHDRAWN,
    ApplicationState.DUPLICATE_APPLICATION_BLOCKED,
}

_CRITICAL_ANSWER_KEYS = ("full_name", "email", "phone", "do_you_require_sponsorship")


@dataclass
class EligibilityResult:
    enters_queue: bool
    auto_submit_eligible: bool
    employment_type: EmploymentType
    reasons: list[str] = field(default_factory=list)
    hard_skip: bool = False
    blocking_category: str = ""

    def as_dict(self) -> dict:
        return {
            "enters_queue": self.enters_queue, "auto_submit_eligible": self.auto_submit_eligible,
            "employment_type": self.employment_type.value, "reasons": self.reasons,
            "hard_skip": self.hard_skip, "blocking_category": self.blocking_category,
        }


def _blocked(reason: str, category: str, *, hard_skip: bool = False) -> EligibilityResult:
    employment_type = EmploymentType.UNKNOWN
    return EligibilityResult(
        enters_queue=False, auto_submit_eligible=False, employment_type=employment_type,
        reasons=[reason], hard_skip=hard_skip, blocking_category=category,
    )


def _answers_complete_enough(answers_path: str | None) -> tuple[bool, str]:
    if not answers_path:
        return False, "application_answers.json not generated yet"
    try:
        with open(answers_path) as fh:
            answers = json.load(fh)
    except (OSError, ValueError):
        return False, "application_answers.json missing or unreadable"
    missing = [k for k in _CRITICAL_ANSWER_KEYS if answers.get(k) in (None, "", NEEDS_USER_INPUT)]
    if missing:
        return False, f"critical application answers missing: {', '.join(missing)}"
    return True, ""


def evaluate_executor_eligibility(job: Job) -> EligibilityResult:
    """ALL must be true for `enters_queue`; ALL of those plus sponsorship ==
    CONFIRMED_SPONSOR plus a PERMITTED_AUTO provider policy are additionally
    required for `auto_submit_eligible` (checked again, independently, by
    app.applications.executor at submit time -- this function only clears the
    job-level portion of the gate)."""
    reasons: list[str] = []

    # --- CLAUDE.md Phase 8 section 1: FULL_TIME hard gate, unconditional. ---
    employment_type = classify_employment_type(job.employment_type, job.title, job.description)
    if employment_type not in (EmploymentType.FULL_TIME, EmploymentType.UNKNOWN):
        return EligibilityResult(
            enters_queue=False, auto_submit_eligible=False, employment_type=employment_type,
            reasons=[f"Application blocked: employment type is {employment_type.value}."],
            hard_skip=True, blocking_category="EMPLOYMENT_TYPE",
        )
    if employment_type == EmploymentType.UNKNOWN:
        reasons.append("Employment type not positively confirmed FULL_TIME -- ASSIST-only, never auto-submit.")

    if not is_us_location(job.location):
        return EligibilityResult(
            enters_queue=False, auto_submit_eligible=False, employment_type=employment_type,
            reasons=["Job location is not US-based."], hard_skip=True, blocking_category="LOCATION",
        )

    is_target, _is_primary = is_target_role(job.title)
    if not is_target:
        return EligibilityResult(
            enters_queue=False, auto_submit_eligible=False, employment_type=employment_type,
            reasons=["Not a CS/STEM target role."], hard_skip=True, blocking_category="ROLE",
        )

    seniority_ok, seniority_reason, _ = evaluate_seniority(job.title, job.description)
    if not seniority_ok:
        return EligibilityResult(
            enters_queue=False, auto_submit_eligible=False, employment_type=employment_type,
            reasons=[seniority_reason], hard_skip=True, blocking_category="SENIORITY",
        )

    compensation_ok, compensation_reason = evaluate_compensation(job.salary_min, job.salary_max)
    if not compensation_ok:
        return EligibilityResult(
            enters_queue=False, auto_submit_eligible=False, employment_type=employment_type,
            reasons=[compensation_reason], hard_skip=True, blocking_category="COMPENSATION",
        )

    if job.technical_match_score < config.MIN_APPLICATION_MATCH_SCORE:
        return EligibilityResult(
            enters_queue=False, auto_submit_eligible=False, employment_type=employment_type,
            reasons=[f"technical match {job.technical_match_score}% below application threshold "
                     f"({config.MIN_APPLICATION_MATCH_SCORE}%)."],
            hard_skip=True, blocking_category="MATCH_SCORE",
        )

    if job.sponsorship_status == SponsorshipStatus.NO_SPONSORSHIP:
        return EligibilityResult(
            enters_queue=False, auto_submit_eligible=False, employment_type=employment_type,
            reasons=["NO_SPONSORSHIP -- hard skip."], hard_skip=True, blocking_category="SPONSORSHIP",
        )
    if job.sponsorship_status == SponsorshipStatus.UNKNOWN:
        return EligibilityResult(
            enters_queue=False, auto_submit_eligible=False, employment_type=employment_type,
            reasons=["Sponsorship UNKNOWN -- do not apply per policy."], hard_skip=False,
            blocking_category="SPONSORSHIP",
        )

    auto_submit_eligible = job.sponsorship_status == SponsorshipStatus.CONFIRMED_SPONSOR
    if job.sponsorship_status == SponsorshipStatus.LIKELY_SPONSOR:
        reasons.append("LIKELY_SPONSOR -- REVIEW_REQUIRED, never auto-submitted.")
        auto_submit_eligible = False

    if job.application_state in _TERMINAL_SKIP_STATES or job.application_state == ApplicationState.APPLIED:
        return EligibilityResult(
            enters_queue=False, auto_submit_eligible=False, employment_type=employment_type,
            reasons=[f"job is in terminal/skip state {job.application_state.value}."],
            hard_skip=True, blocking_category="STATE",
        )

    if job.application_state not in (ApplicationState.READY_TO_APPLY, ApplicationState.REVIEW_REQUIRED,
                                      ApplicationState.EXECUTION_QUEUED, ApplicationState.NEEDS_USER_ACTION,
                                      ApplicationState.APPROVED,
                                      ApplicationState.SUBMISSION_STATUS_UNKNOWN, ApplicationState.SUBMISSION_FAILED):
        return EligibilityResult(
            enters_queue=False, auto_submit_eligible=False, employment_type=employment_type,
            reasons=[f"job has not completed resume/answer generation (state={job.application_state.value})."],
            hard_skip=False, blocking_category="NOT_READY",
        )

    if not job.resume_docx_path or not job.resume_pdf_path:
        return EligibilityResult(
            enters_queue=False, auto_submit_eligible=False, employment_type=employment_type,
            reasons=["resume artifacts not generated for this job."], hard_skip=False, blocking_category="RESUME",
        )

    answers_ok, answers_reason = _answers_complete_enough(job.application_answers_path)
    if not answers_ok:
        reasons.append(answers_reason)
        auto_submit_eligible = False

    if employment_type == EmploymentType.UNKNOWN:
        auto_submit_eligible = False

    return EligibilityResult(
        enters_queue=True, auto_submit_eligible=auto_submit_eligible, employment_type=employment_type,
        reasons=reasons, hard_skip=False, blocking_category="" if auto_submit_eligible else "REVIEW",
    )
