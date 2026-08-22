"""Resume/JD/answers linkage check (CLAUDE.md Phase 13 sections 43-45).

Before a real ATS upload, four things must agree:
  - the job's current JD fingerprint (`job.jd_sponsorship_fingerprint`,
    already computed by app.sponsorship.decision at analysis time)
  - the JD fingerprint the CURRENTLY generated resume was built against
    (`job.resume_jd_fingerprint`, set by app.pipeline.generate_assist_outputs)
  - the resume artifact's own existence/ownership (already checked by
    app.applications.browser_assist._verify_resume's file/hash checks)
  - the answer-snapshot version recorded on the browser session
    (`browser_assist_sessions.answers_version`, already tracked since Phase 10)

This module only adds the missing piece: whether the JD has materially moved
on since the resume was generated. It never regenerates anything itself
(CLAUDE.md section 44 "regenerate/revalidate package" is a pipeline
responsibility) -- it only reports staleness so a caller can refuse to
upload and instead ask for regeneration."""

from dataclasses import dataclass

from app.models import Job


@dataclass(frozen=True)
class ResumeFreshnessResult:
    fresh: bool
    reason: str = ""


def verify_resume_freshness(job: Job) -> ResumeFreshnessResult:
    """Returns fresh=False ONLY when a CONFIRMED divergence is observable:
    both `resume_jd_fingerprint` (recorded when the resume was generated)
    and the job's current `jd_sponsorship_fingerprint` are non-empty AND
    they differ. A resume with no recorded generation fingerprint (e.g.
    generated before this tracking existed, or a job constructed directly
    rather than through the normal pipeline) is reported fresh=True with a
    note -- this module only ever flags a CONFIRMED divergence, never a
    guessed one from missing data (matching this project's standing 'never
    fabricate confidence' rule)."""
    if not job.resume_jd_fingerprint:
        return ResumeFreshnessResult(
            fresh=True, reason="no resume-generation fingerprint recorded -- nothing to compare against, "
                                "assuming fresh",
        )
    if not job.jd_sponsorship_fingerprint:
        return ResumeFreshnessResult(
            fresh=True, reason="job has no current JD fingerprint recorded -- nothing to compare against, "
                                "assuming fresh",
        )
    if job.resume_jd_fingerprint == job.jd_sponsorship_fingerprint:
        return ResumeFreshnessResult(fresh=True, reason="resume was generated against the current JD fingerprint")
    return ResumeFreshnessResult(
        fresh=False,
        reason="the job description changed since this resume was generated "
               f"(resume built for fingerprint '{job.resume_jd_fingerprint[:12]}...', "
               f"job is now at '{job.jd_sponsorship_fingerprint[:12]}...') -- regenerate before uploading",
    )
