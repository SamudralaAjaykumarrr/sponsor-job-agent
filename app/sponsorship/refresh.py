"""Evidence-only sponsorship refresh (Sponsorship Intelligence Coverage V1).

`app.pipeline.analyze_job()` recomputes sponsorship AND advances
`application_state`/priority/freshness/match-score together -- correct for a
freshly-discovered job, but unsafe to call again on a job that has already
progressed past initial analysis (it would silently reset application_state
back to ANALYZED/SKIPPED_*, destroying any resume-generation or
browser-assist progress already made on that job).

`app.pipeline.reanalyze_job()` is safe but a deliberate no-op unless the JD
TEXT itself changed -- it never re-evaluates a job just because NEW EMPLOYER
EVIDENCE became available (this feature's whole point).

`refresh_job_sponsorship()` fills that gap: it recomputes the sponsorship
decision from current evidence via the unmodified
app.sponsorship.decision.persist_decision() and writes ONLY the
sponsorship-related columns on the job row -- never application_state, never
priority/score/freshness/resume fields. Safe to call on a job at any stage of
its lifecycle, including one already mid-application, without disturbing its
progress in any way."""

from dataclasses import dataclass

from app.jobs_repo import get_job, update_job
from app.models import SponsorshipStatus
from app.sponsorship.decision import SponsorshipDecision, persist_decision


@dataclass
class SponsorshipRefreshOutcome:
    job_id: int
    previous_status: SponsorshipStatus
    new_status: SponsorshipStatus
    decision: SponsorshipDecision

    @property
    def changed(self) -> bool:
        return self.previous_status != self.new_status


def refresh_job_sponsorship(job_id: int) -> SponsorshipRefreshOutcome:
    job = get_job(job_id)
    if job is None:
        raise ValueError(f"job {job_id} not found")

    previous_status = job.sponsorship_status
    decision = persist_decision(job_id, job.title, job.company, job.description, job.state)

    update_job(
        job_id,
        sponsorship_status=decision.status,
        sponsorship_evidence=decision.evidence_text,
        sponsorship_decision_version=decision.decision_version,
        jd_sponsorship_fingerprint=decision.jd_fingerprint,
        sponsorship_conflict=decision.conflict,
        sponsorship_blocking_reason=decision.blocking_reason,
    )

    return SponsorshipRefreshOutcome(
        job_id=job_id, previous_status=previous_status, new_status=decision.status, decision=decision,
    )
