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
    # Tracker board (premium UI): Applied -> Assessment/Interview/Rejected/Withdrawn,
    # Assessment -> Interview/Rejected/Withdrawn, Interview -> Offer/Rejected/Withdrawn/
    # back to Applied, Offer -> Rejected (declined)/Withdrawn. Manual-only, mirroring
    # every other tracker transition in this table -- nothing here is set automatically
    # by the pipeline/executor.
    ApplicationState.APPLIED: {
        ApplicationState.ASSESSMENT, ApplicationState.INTERVIEW,
        ApplicationState.REJECTED, ApplicationState.WITHDRAWN,
    },
    ApplicationState.ASSESSMENT: {
        ApplicationState.INTERVIEW, ApplicationState.REJECTED, ApplicationState.WITHDRAWN,
    },
    ApplicationState.INTERVIEW: {
        ApplicationState.OFFER, ApplicationState.REJECTED,
        ApplicationState.WITHDRAWN, ApplicationState.APPLIED,
    },
    ApplicationState.OFFER: {ApplicationState.REJECTED, ApplicationState.WITHDRAWN},
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
    # Approval-gated-autonomy-v1: an APPROVED job (provider has no verified
    # auto-submit capability) is typically finished via browser-assist or
    # manually outside the executor -- same "let a human record what they
    # truthfully did" pattern as NEEDS_USER_ACTION/SUBMISSION_STATUS_UNKNOWN
    # above.
    ApplicationState.APPROVED: {
        ApplicationState.APPLIED, ApplicationState.SKIPPED, ApplicationState.WITHDRAWN,
    },
}


def can_transition(current: ApplicationState, target: ApplicationState) -> bool:
    return target in ALLOWED_MANUAL_TRANSITIONS.get(current, set())


def valid_manual_transitions(current: ApplicationState) -> list[ApplicationState]:
    """Premium UI: the job detail page's 'Update state' control only ever
    offers targets that are actually legal from the job's current state --
    CLAUDE.md's 'every visible button must work, no decorative/no-op
    buttons' extended to this dropdown (previously it always listed the
    full fixed option set regardless of current state, so most selections
    from most states silently 400'd)."""
    return sorted(ALLOWED_MANUAL_TRANSITIONS.get(current, set()), key=lambda s: s.value)
