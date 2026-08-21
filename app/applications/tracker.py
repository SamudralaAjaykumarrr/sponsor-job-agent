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
}


def can_transition(current: ApplicationState, target: ApplicationState) -> bool:
    return target in ALLOWED_MANUAL_TRANSITIONS.get(current, set())
