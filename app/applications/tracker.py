from app.models import ApplicationState

# Manual-only transitions a user can make from the dashboard. Pipeline-driven
# transitions (DISCOVERED/NEW -> ANALYZED -> READY_TO_APPLY/REVIEW_REQUIRED,
# or -> a SKIPPED_* state) happen automatically inside pipeline.py/agent/cycle.py
# and are not listed here.
ALLOWED_MANUAL_TRANSITIONS = {
    ApplicationState.READY_TO_APPLY: {ApplicationState.APPLIED, ApplicationState.SKIPPED},
    ApplicationState.REVIEW_REQUIRED: {
        ApplicationState.READY_TO_APPLY, ApplicationState.APPLIED, ApplicationState.SKIPPED,
    },
    ApplicationState.APPLIED: {ApplicationState.INTERVIEW, ApplicationState.REJECTED},
    ApplicationState.INTERVIEW: {ApplicationState.REJECTED, ApplicationState.APPLIED},
    ApplicationState.ANALYZED: {ApplicationState.SKIPPED},
    ApplicationState.CLAIM_VALIDATION_FAILED: {ApplicationState.SKIPPED},
    # --- Phase 8 (CLAUDE.md Phase 8 sections 40, 43): the human review queue's
    # manual actions. "Mark Applied Manually" is deliberately still available
    # even from NEEDS_USER_ACTION/SUBMISSION_STATUS_UNKNOWN -- a human who
    # completed the application themselves outside the executor (e.g. after a
    # CAPTCHA stop) should be able to record that truthfully. There is no
    # "force submit" transition anywhere in this table.
    ApplicationState.EXECUTION_QUEUED: {ApplicationState.APPLIED, ApplicationState.SKIPPED, ApplicationState.WITHDRAWN},
    ApplicationState.NEEDS_USER_ACTION: {
        ApplicationState.READY_TO_APPLY, ApplicationState.APPLIED, ApplicationState.SKIPPED,
        ApplicationState.WITHDRAWN,
    },
    ApplicationState.SUBMITTING: {ApplicationState.APPLIED, ApplicationState.SUBMISSION_STATUS_UNKNOWN},
    ApplicationState.SUBMISSION_STATUS_UNKNOWN: {
        ApplicationState.APPLIED, ApplicationState.SKIPPED, ApplicationState.WITHDRAWN,
    },
    ApplicationState.SUBMISSION_FAILED: {ApplicationState.READY_TO_APPLY, ApplicationState.SKIPPED},
    ApplicationState.DUPLICATE_APPLICATION_BLOCKED: {ApplicationState.SKIPPED},
}


def can_transition(current: ApplicationState, target: ApplicationState) -> bool:
    return target in ALLOWED_MANUAL_TRANSITIONS.get(current, set())
