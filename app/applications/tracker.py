from app.models import ApplicationState

# Manual-only transitions a user can make from the dashboard once a job is
# READY_TO_APPLY. Pipeline-driven transitions (NEW -> ANALYZED -> READY_TO_APPLY,
# or -> SKIPPED) happen automatically inside pipeline.py and are not listed here.
ALLOWED_MANUAL_TRANSITIONS = {
    ApplicationState.READY_TO_APPLY: {ApplicationState.APPLIED, ApplicationState.SKIPPED},
    ApplicationState.APPLIED: {ApplicationState.INTERVIEW, ApplicationState.REJECTED},
    ApplicationState.INTERVIEW: {ApplicationState.REJECTED, ApplicationState.APPLIED},
    ApplicationState.ANALYZED: {ApplicationState.SKIPPED},
}


def can_transition(current: ApplicationState, target: ApplicationState) -> bool:
    return target in ALLOWED_MANUAL_TRANSITIONS.get(current, set())
