"""Global system doctor (CLAUDE.md Phase 15 section 10): `python -m app.doctor`.

Aggregates every existing subsystem doctor -- app.registry.doctor,
app.sponsorship.doctor, app.applications.doctor, app.resume_optimizer.doctor
-- rather than re-implementing their checks (Phase 15 section 10: "Reuse
existing doctors rather than duplicating logic"). Those four doctors already
cover the state-consistency invariants Phase 15 section 9 calls out by name
(APPLIED without confirmation, READY_TO_APPLY with a stale resume,
NO_SPONSORSHIP with an active execution, non-FULL_TIME with an active
application) -- see docs/release-candidate-audit.md for the full mapping.

This module adds only checks that don't already belong to one of those four
subsystems: database/schema reachability, candidate profile availability,
configuration validity (app.config_doctor), a light job-integrity pass, and
a dead-letter backlog signal. Read-only, like every other doctor in this
project -- never auto-repairs anything."""

import sys
from dataclasses import dataclass, field


@dataclass
class Issue:
    severity: str  # "serious" | "warning"
    source: str    # which subsystem/check produced this
    check: str
    detail: str


@dataclass
class GlobalDoctorReport:
    issues: list[Issue] = field(default_factory=list)
    subsystems_run: list[str] = field(default_factory=list)
    subsystems_skipped: list[str] = field(default_factory=list)

    @property
    def serious_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "serious")

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "warning")

    def as_dict(self) -> dict:
        return {
            "serious_count": self.serious_count,
            "warning_count": self.warning_count,
            "subsystems_run": self.subsystems_run,
            "subsystems_skipped": self.subsystems_skipped,
            "issues": [
                {"severity": i.severity, "source": i.source, "check": i.check, "detail": i.detail}
                for i in self.issues
            ],
        }


def _absorb(report: GlobalDoctorReport, source: str, sub_report) -> None:
    for issue in sub_report.issues:
        report.issues.append(Issue(issue.severity, source, issue.check, issue.detail))
    report.subsystems_run.append(source)


def _run_subsystem_doctor(report: GlobalDoctorReport, source: str, run_fn) -> None:
    try:
        _absorb(report, source, run_fn())
    except Exception as exc:  # noqa: BLE001 -- one subsystem's doctor failing must never abort the rest
        report.issues.append(Issue("serious", source, "doctor_crashed", f"{source} doctor raised {type(exc).__name__}: {exc}"))
        report.subsystems_skipped.append(source)


def _check_database_and_schema(report: GlobalDoctorReport) -> None:
    from app.health import check_readiness

    result = check_readiness()
    report.subsystems_run.append("database")
    if not result.database_reachable:
        report.issues.append(Issue("serious", "database", "database_unreachable", result.detail))
        return
    if not result.schema_compatible:
        report.issues.append(Issue("serious", "database", "schema_incompatible", result.detail))


def _check_candidate_profile(report: GlobalDoctorReport) -> None:
    from app.candidate.profile import load_profile, missing_fields

    report.subsystems_run.append("candidate_profile")
    try:
        profile = load_profile()
    except Exception as exc:  # noqa: BLE001
        report.issues.append(Issue("serious", "candidate_profile", "profile_load_failed",
                                    f"could not load candidate_data/profile.json: {type(exc).__name__}: {exc}"))
        return
    missing = missing_fields(profile)
    if missing:
        report.issues.append(Issue("warning", "candidate_profile", "profile_incomplete",
                                    f"{len(missing)} field(s) still NEEDS_USER_INPUT: {', '.join(missing[:10])}"
                                    + (" ..." if len(missing) > 10 else "")))
    if not profile.employment and not profile.projects:
        report.issues.append(Issue("warning", "candidate_profile", "no_evidence",
                                    "profile has no employment history and no projects -- resume generation will "
                                    "have essentially no verified evidence to draw from."))


def _check_config(report: GlobalDoctorReport) -> None:
    from app.config_doctor import run_config_doctor

    _run_subsystem_doctor(report, "config", run_config_doctor)


def _check_job_integrity(report: GlobalDoctorReport) -> None:
    """Basic sanity pass not owned by any subsystem doctor: a job row with
    an empty title/company, or an application_state value that isn't a
    known ApplicationState, indicates a data-integrity problem upstream of
    every other doctor (which all assume a job row is at least well-formed)."""
    from app.db import db_session
    from app.models import ApplicationState

    report.subsystems_run.append("job_integrity")
    valid_states = {s.value for s in ApplicationState}
    with db_session() as conn:
        blank = conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE title = '' OR company = ''"
        ).fetchone()["c"]
        if blank:
            report.issues.append(Issue("warning", "job_integrity", "blank_title_or_company",
                                        f"{blank} job row(s) have an empty title or company."))
        rows = conn.execute("SELECT DISTINCT application_state FROM jobs").fetchall()
        unknown = [r["application_state"] for r in rows if r["application_state"] not in valid_states]
        if unknown:
            report.issues.append(Issue("serious", "job_integrity", "unknown_application_state",
                                        f"job rows exist with unrecognized application_state value(s): {unknown}"))


def _check_dead_letter_backlog(report: GlobalDoctorReport) -> None:
    """Informational only -- a nonzero dead-letter backlog is expected
    operational signal, not itself a bug; surfaced here as a warning so it's
    never silently invisible to an operator running the global doctor."""
    from app.workers.dead_letter import list_dead_letters

    report.subsystems_run.append("dead_letter_backlog")
    try:
        backlog = list_dead_letters(limit=1000)
    except Exception:  # noqa: BLE001 -- table may not exist yet on a very old schema; not fatal here
        return
    if backlog:
        report.issues.append(Issue("warning", "dead_letter_backlog", "nonzero_backlog",
                                    f"{len(backlog)} dead-lettered portal/provider record(s) awaiting operator review "
                                    "(requeue is always an explicit action -- see app.workers.dead_letter.requeue)."))


def run_global_doctor() -> GlobalDoctorReport:
    report = GlobalDoctorReport()

    _check_database_and_schema(report)
    if report.serious_count and any(i.check == "database_unreachable" for i in report.issues):
        # Every other doctor needs a working DB connection -- report the
        # single root cause honestly rather than a wall of downstream
        # "database unreachable" crashes from four separate subsystems.
        report.subsystems_skipped.extend(
            ["registry", "sponsorship", "applications", "resume_optimizer", "candidate_profile", "job_integrity", "dead_letter_backlog"]
        )
        return report

    _check_config(report)
    _check_candidate_profile(report)
    _check_job_integrity(report)
    _check_dead_letter_backlog(report)

    from app.registry.doctor import run_doctor as run_registry_doctor
    from app.sponsorship.doctor import run_doctor as run_sponsorship_doctor
    from app.applications.doctor import run_doctor as run_applications_doctor
    from app.resume_optimizer.doctor import run_doctor as run_resume_optimizer_doctor

    _run_subsystem_doctor(report, "registry", run_registry_doctor)
    _run_subsystem_doctor(report, "sponsorship", run_sponsorship_doctor)
    _run_subsystem_doctor(report, "applications", run_applications_doctor)
    _run_subsystem_doctor(report, "resume_optimizer", run_resume_optimizer_doctor)

    return report


def main() -> int:
    report = run_global_doctor()
    print(f"Global doctor: {report.serious_count} serious issue(s), {report.warning_count} warning(s) "
          f"across {len(report.subsystems_run)} subsystem check(s).")
    for issue in report.issues:
        print(f"  [{issue.severity.upper():7s}] {issue.source}.{issue.check}: {issue.detail}")
    if report.subsystems_skipped:
        print(f"Skipped: {', '.join(report.subsystems_skipped)}")
    return 1 if report.serious_count else 0


if __name__ == "__main__":
    sys.exit(main())
