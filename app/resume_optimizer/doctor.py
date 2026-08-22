"""Resume optimizer doctor (CLAUDE.md Phase 14 section 66). Read-only,
never auto-repairs -- same contract as app.registry.doctor /
app.sponsorship.doctor / app.applications.doctor."""

from dataclasses import dataclass, field

from app.db import db_session


@dataclass
class Issue:
    severity: str
    check: str
    detail: str


@dataclass
class DoctorReport:
    issues: list[Issue] = field(default_factory=list)

    @property
    def serious_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "serious")

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "warning")

    def as_dict(self) -> dict:
        return {
            "serious_count": self.serious_count, "warning_count": self.warning_count,
            "issues": [{"severity": i.severity, "check": i.check, "detail": i.detail} for i in self.issues],
        }


def _check_resume_linked_to_wrong_job(conn, report: DoctorReport) -> None:
    rows = conn.execute("SELECT variant_id, job_id, resume_docx_path FROM resume_variants WHERE status = 'READY'").fetchall()
    for r in rows:
        if r["resume_docx_path"] and f"/{r['job_id']}/optimized/" not in r["resume_docx_path"].replace("\\", "/"):
            report.issues.append(Issue("serious", "resume_linked_to_wrong_job",
                                        f"variant {r['variant_id']} artifact path does not match job_id {r['job_id']}"))


def _check_jd_fingerprint_mismatch(conn, report: DoctorReport) -> None:
    """CLAUDE.md section 66 'JD fingerprint mismatch': a current READY
    variant whose stored jd_fingerprint no longer matches the job's live
    JD text means the JD changed after generation without the variant being
    marked STALE (should be unreachable given app.pipeline.reanalyze_job's
    mark_stale() call, but checked defensively -- doctor checks exist
    precisely to catch integration gaps like a manual DB edit or a future
    code path that updates jobs.description without going through
    reanalyze_job)."""
    from app.resume_optimizer.fingerprint import compute_jd_fingerprint

    rows = conn.execute(
        """SELECT rv.variant_id, rv.job_id, rv.jd_fingerprint, j.title, j.company, j.description
           FROM resume_variants rv JOIN jobs j ON j.id = rv.job_id
           WHERE rv.current = 1 AND rv.status = 'READY'"""
    ).fetchall()
    for r in rows:
        live_fingerprint = compute_jd_fingerprint(r["title"], r["company"], r["description"])
        if live_fingerprint != r["jd_fingerprint"]:
            report.issues.append(Issue("warning", "jd_fingerprint_mismatch",
                                        f"variant {r['variant_id']} (job {r['job_id']}) was generated against a JD that "
                                        "has since changed but is still marked current/READY -- regenerate."))


def _check_profile_version_mismatch(conn, report: DoctorReport) -> None:
    from app.candidate.profile import load_profile
    from app.resume_optimizer.fingerprint import compute_profile_version

    current_version = compute_profile_version(load_profile())
    rows = conn.execute("SELECT variant_id, job_id, profile_version FROM resume_variants WHERE current = 1 AND status = 'READY'").fetchall()
    for r in rows:
        if r["profile_version"] != current_version:
            report.issues.append(Issue("warning", "stale_profile_version",
                                        f"variant {r['variant_id']} (job {r['job_id']}) was generated against an older candidate profile -- regenerate."))


def _check_claim_checker_failures_current(conn, report: DoctorReport) -> None:
    rows = conn.execute("SELECT variant_id, job_id FROM resume_variants WHERE current = 1 AND status = 'CLAIM_CHECK_FAILED'").fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "claim_checker_failure_current",
                                    f"job {r['job_id']} current variant {r['variant_id']} failed claim validation and was never superseded."))


def _check_unsupported_skill_inserted(conn, report: DoctorReport) -> None:
    from app.candidate.profile import load_profile
    from app.resume_optimizer.models import SKILL_CATEGORIES

    verified = {s.lower() for s in load_profile().skills}
    skill_category_names = {c.value for c in SKILL_CATEGORIES}
    rows = conn.execute(
        """SELECT rv.job_id, rel.variant_id, rel.requirement_text FROM resume_evidence_links rel
           JOIN resume_variants rv ON rv.variant_id = rel.variant_id
           WHERE rel.status = 'MATCHED' AND rel.requirement_category IN ({})""".format(
            ",".join("?" for _ in skill_category_names)
        ),
        list(skill_category_names),
    ).fetchall()
    for r in rows:
        # A MATCHED skill requirement's text must always trace back to a
        # verified profile skill -- this is the doctor-level truthfulness
        # firewall behind CLAUDE.md section 66's "unsupported skill inserted"
        # check, distinct from (and in addition to) the resume-content-level
        # claim_checker firewall in app.resume.claim_checker.
        if r["requirement_text"].lower() not in verified:
            report.issues.append(Issue("serious", "unsupported_skill_inserted",
                                        f"variant {r['variant_id']} (job {r['job_id']}) marked '{r['requirement_text']}' MATCHED "
                                        "but it is not a verified candidate skill."))


def _check_missing_artifact(conn, report: DoctorReport) -> None:
    import os

    rows = conn.execute("SELECT variant_id, job_id, resume_docx_path, resume_pdf_path, resume_txt_path FROM resume_variants WHERE status = 'READY'").fetchall()
    for r in rows:
        for key in ("resume_docx_path", "resume_pdf_path", "resume_txt_path"):
            path = r[key]
            if not path or not os.path.exists(path):
                report.issues.append(Issue("serious", "missing_artifact",
                                            f"variant {r['variant_id']} (job {r['job_id']}) is READY but {key} is missing on disk."))


def _check_quality_report_missing(conn, report: DoctorReport) -> None:
    rows = conn.execute(
        """SELECT rv.variant_id, rv.job_id FROM resume_variants rv
           LEFT JOIN resume_quality_reports qr ON qr.variant_id = rv.variant_id
           WHERE rv.status = 'READY' AND qr.variant_id IS NULL"""
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "quality_report_missing",
                                    f"variant {r['variant_id']} (job {r['job_id']}) is READY with no quality report."))


def _check_stale_variant_marked_current(conn, report: DoctorReport) -> None:
    rows = conn.execute("SELECT variant_id, job_id FROM resume_variants WHERE current = 1 AND status = 'STALE'").fetchall()
    for r in rows:
        report.issues.append(Issue("warning", "stale_variant_marked_current",
                                    f"job {r['job_id']} current variant {r['variant_id']} is STALE -- regenerate before applying."))


def _check_parse_failure_current(conn, report: DoctorReport) -> None:
    rows = conn.execute("SELECT variant_id, job_id FROM resume_variants WHERE current = 1 AND status = 'ATS_PARSE_FAILED'").fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "ats_parse_failure_current",
                                    f"job {r['job_id']} current variant {r['variant_id']} failed ATS parse validation."))


def _check_duplicate_current(conn, report: DoctorReport) -> None:
    rows = conn.execute(
        "SELECT job_id, COUNT(*) AS c FROM resume_variants WHERE current = 1 GROUP BY job_id HAVING COUNT(*) > 1"
    ).fetchall()
    for r in rows:
        report.issues.append(Issue("serious", "duplicate_current_variant",
                                    f"job {r['job_id']} has {r['c']} variants marked current -- unique index should prevent this."))


def run_doctor() -> DoctorReport:
    report = DoctorReport()
    with db_session() as conn:
        _check_resume_linked_to_wrong_job(conn, report)
        _check_jd_fingerprint_mismatch(conn, report)
        _check_profile_version_mismatch(conn, report)
        _check_claim_checker_failures_current(conn, report)
        _check_unsupported_skill_inserted(conn, report)
        _check_missing_artifact(conn, report)
        _check_quality_report_missing(conn, report)
        _check_stale_variant_marked_current(conn, report)
        _check_parse_failure_current(conn, report)
        _check_duplicate_current(conn, report)
    return report
