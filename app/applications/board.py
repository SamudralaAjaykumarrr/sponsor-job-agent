"""Consumer-facing "My Applications" board (application-lifecycle-exception-
resume-v1). Pure read-side view-model, the consumer analog of
app.applications.cta/app.applications.product_state -- never re-derives
stage logic, only groups the SAME authoritative stage/CTA those modules
already compute into the five plain-language categories the product spec
asks for: In Progress / Needs Action / Ready to Apply / Submitted / Issues.

Never shown here: TEST MODE / demo fixtures (`jobs.is_test_fixture = 1`) --
matches app.applications.repo.list_executions_with_jobs's own existing
`is_test_fixture = 0` convention for the primary Applications page. Demo
scenarios are visualized on their own `/demo` page instead."""

from dataclasses import dataclass, field
from typing import Optional

from app.applications import blockers, browser_session, repo
from app.applications.cta import compute_apply_cta
from app.applications.product_state import ProductStage, compute_stage

BUCKET_IN_PROGRESS = "in_progress"
BUCKET_NEEDS_ACTION = "needs_action"
BUCKET_READY_TO_APPLY = "ready_to_apply"
BUCKET_SUBMITTED = "submitted"
BUCKET_ISSUES = "issues"

BUCKET_LABELS: dict[str, str] = {
    BUCKET_IN_PROGRESS: "In Progress",
    BUCKET_NEEDS_ACTION: "Needs Action",
    BUCKET_READY_TO_APPLY: "Ready to Apply",
    BUCKET_SUBMITTED: "Submitted",
    BUCKET_ISSUES: "Issues",
}

_SUBMITTED_STAGES = frozenset({ProductStage.SUBMITTED, ProductStage.CONFIRMED, ProductStage.COMPLETED_BY_USER})
_IN_PROGRESS_STAGES = frozenset({
    ProductStage.DISCOVERED, ProductStage.ELIGIBILITY_CHECKED, ProductStage.JD_ANALYZED,
    ProductStage.RESUME_GENERATING, ProductStage.RESUME_READY, ProductStage.APPLICATION_PREPARING,
    ProductStage.FORM_FILLING, ProductStage.APPROVED, ProductStage.SUBMITTING,
})
_NEEDS_ACTION_STAGES = frozenset({
    ProductStage.NEEDS_USER_INPUT, ProductStage.NEEDS_AUTH, ProductStage.NEEDS_CAPTCHA,
    ProductStage.NEEDS_LEGAL_CONFIRMATION, ProductStage.IDENTITY_REVIEW_REQUIRED,
    ProductStage.UNSUPPORTED_SUBMISSION, ProductStage.SUBMISSION_STATUS_UNKNOWN,
})


@dataclass
class BoardCard:
    job_id: int
    execution_id: str
    title: str
    company: str
    location: str
    stage_label: str
    updated_at: str
    cta: dict  # {label, style, action, reason} -- same shape app.applications.cta.JobCTA.as_dict() returns
    blocker: Optional[dict] = None  # human_title/human_message/required_action/blocker_code, or None

    def as_dict(self) -> dict:
        return {
            "job_id": self.job_id, "execution_id": self.execution_id, "title": self.title,
            "company": self.company, "location": self.location, "stage_label": self.stage_label,
            "updated_at": self.updated_at, "cta": self.cta, "blocker": self.blocker,
        }


def _bucket_for(stage: ProductStage, active_blocker: Optional[dict]) -> str:
    if active_blocker is not None:
        return BUCKET_ISSUES if active_blocker["blocker_class"] == blockers.BlockerClass.TERMINAL.value \
            else BUCKET_NEEDS_ACTION
    if stage == ProductStage.READY_FOR_APPROVAL:
        return BUCKET_READY_TO_APPLY
    if stage in _SUBMITTED_STAGES:
        return BUCKET_SUBMITTED
    if stage in _NEEDS_ACTION_STAGES:
        # Defensive fallback for an execution whose blocker row is missing
        # (e.g. data predating this feature) -- the live stage is still the
        # authoritative source of truth for the CTA either way.
        return BUCKET_NEEDS_ACTION
    if stage == ProductStage.TRACKING:
        return BUCKET_ISSUES
    if stage in _IN_PROGRESS_STAGES:
        return BUCKET_IN_PROGRESS
    return BUCKET_IN_PROGRESS


def build_board(limit: int = 500) -> dict[str, list[dict]]:
    executions = repo.list_executions_with_jobs(limit=limit)
    job_ids = [row["job_id"] for row in executions]
    sessions_by_job = browser_session.get_active_sessions_for_jobs(job_ids) if job_ids else {}

    buckets: dict[str, list[dict]] = {key: [] for key in BUCKET_LABELS}
    for row in executions:
        stage_info = compute_stage(row)
        active_blocker = blockers.get_active_blocker_for_execution(row["execution_id"])
        session = sessions_by_job.get(row["job_id"])
        cta = compute_apply_cta(row["job_id"], None, execution=row, browser_session=session)
        blocker_view = None
        if active_blocker is not None:
            blocker_view = {
                "blocker_code": active_blocker["blocker_code"],
                "human_title": active_blocker["human_title"],
                "human_message": active_blocker["human_message"],
                "required_action": active_blocker["required_action"],
                "created_at": active_blocker["created_at"],
            }
        card = BoardCard(
            job_id=row["job_id"], execution_id=row["execution_id"], title=row.get("job_title") or "",
            company=row.get("job_company") or "", location=row.get("job_location") or "",
            stage_label=stage_info.label, updated_at=row.get("updated_at") or row.get("started_at") or "",
            cta=cta.as_dict(), blocker=blocker_view,
        )
        bucket = _bucket_for(stage_info.stage, active_blocker)
        buckets[bucket].append(card.as_dict())

    for cards in buckets.values():
        cards.sort(key=lambda c: c["updated_at"], reverse=True)
    return buckets


def bucket_counts(buckets: dict[str, list[dict]]) -> dict[str, int]:
    return {key: len(cards) for key, cards in buckets.items()}
