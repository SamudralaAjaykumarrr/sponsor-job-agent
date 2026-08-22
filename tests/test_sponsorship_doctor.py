"""CLAUDE.md Phase 7 section 35: sponsorship doctor integrity checks."""

from app.db import db_session
from app.registry.models import Company
from app.registry import store
from app.sponsorship.aliases import add_alias
from app.sponsorship.doctor import run_doctor
from app.sponsorship.evidence import SponsorshipEvidence, record_evidence
from app.sponsorship.identity import resolve_company
from app.sponsorship.relationships import add_relationship
from app.sponsorship.schema import AliasType, RelationshipType


def test_clean_database_has_no_serious_issues(tmp_env):
    report = run_doctor()
    assert report.serious_count == 0


def test_orphan_evidence_detected(tmp_env):
    with db_session() as conn:
        conn.execute(
            "INSERT INTO employer_sponsorship_evidence (company_id, company_name_raw, source, observed_at, imported_at) "
            "VALUES (99999, 'GhostCo', 'test', '2024-01-01', '2024-01-01')"
        )
    report = run_doctor()
    assert any(i.check == "orphan_evidence_company_id" for i in report.issues)
    assert report.serious_count >= 1


def test_invalid_fiscal_year_detected(tmp_env):
    record_evidence(SponsorshipEvidence(company_name_raw="BadYearCo", fiscal_year=1800))
    report = run_doctor()
    assert any(i.check == "invalid_fiscal_year" for i in report.issues)


def test_verified_alias_collision_detected(tmp_env):
    c1 = store.insert_company(Company(normalized_name="c1", display_name="C1", primary_domain="c1.com"))
    c2 = store.insert_company(Company(normalized_name="c2", display_name="C2", primary_domain="c2.com"))
    add_alias(c1, "Shared", AliasType.BRAND_NAME, verified=True)
    add_alias(c2, "Shared", AliasType.BRAND_NAME, verified=True)
    report = run_doctor()
    assert any(i.check == "verified_alias_collision" for i in report.issues)


def test_relationship_contradiction_detected(tmp_env):
    c1 = store.insert_company(Company(normalized_name="p1", display_name="P1", primary_domain="p1.com"))
    c2 = store.insert_company(Company(normalized_name="p2", display_name="P2", primary_domain="p2.com"))
    add_relationship(c1, c2, RelationshipType.PARENT, verified=True)
    add_relationship(c2, c1, RelationshipType.PARENT, verified=True)
    report = run_doctor()
    assert any(i.check == "parent_subsidiary_contradiction" for i in report.issues)


def test_confirmed_without_current_evidence_detected(tmp_env):
    from app.sponsorship.decision import persist_decision

    persist_decision(1234, "Engineer", "SomeCo", "We sponsor H-1B visas for this role.", "CA")
    with db_session() as conn:
        conn.execute("UPDATE sponsorship_decisions SET current_job_evidence = '[]' WHERE job_id = 1234")
    report = run_doctor()
    assert any(i.check == "confirmed_without_current_evidence" for i in report.issues)


def test_no_sponsorship_hard_skip_violation_detected(tmp_env):
    from app.models import Job

    from app.jobs_repo import insert_job

    job = Job(title="Engineer", company="BadStateCo", description="no sponsorship", location="",
              sponsorship_status="NO_SPONSORSHIP", application_state="READY_TO_APPLY")
    insert_job(job)
    report = run_doctor()
    assert any(i.check == "no_sponsorship_not_hard_skipped" for i in report.issues)


def test_pending_identity_review_is_warning_not_serious(tmp_env):
    store.insert_company(Company(normalized_name="ambigco", display_name="AmbigCo", primary_domain="a1.com"))
    store.insert_company(Company(normalized_name="ambigco", display_name="AmbigCo", primary_domain="a2.com"))
    resolve_company("AmbigCo")
    report = run_doctor()
    warning = [i for i in report.issues if i.check == "pending_identity_review_backlog"]
    assert len(warning) == 1
    assert warning[0].severity == "warning"
