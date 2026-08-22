"""CLAUDE.md Phase 7 section 39: run sponsorship intelligence tests against
real PostgreSQL too. Marked `postgres` -- only run via `pytest -m postgres`,
automatically skipped if `pgserver` isn't installed (see
tests/conftest.py::postgres_url)."""

import pytest

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg_db(pg_fresh_db, monkeypatch):
    import app.db as db

    monkeypatch.setattr(db, "DATABASE_URL", pg_fresh_db)
    assert db.backend() == "postgres"
    db.init_db()
    return db


def test_sponsorship_tables_created(pg_db):
    with pg_db.db_session() as conn:
        for table in (
            "employer_sponsorship_evidence", "sponsorship_datasets", "company_aliases",
            "company_relationships", "employer_sponsorship_profile", "sponsorship_decisions",
            "employer_identity_review",
        ):
            row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
            assert row["c"] == 0


def test_evidence_insert_and_profile_compute_on_postgres(pg_db):
    import datetime

    from app.registry.models import Company
    from app.registry import store
    from app.sponsorship.evidence import SponsorshipEvidence, record_evidence
    from app.sponsorship.profile import refresh_employer_profile

    this_year = datetime.datetime.now(datetime.timezone.utc).year
    cid = store.insert_company(Company(normalized_name="pgco", display_name="PgCo", primary_domain="pgco.com"))
    record_evidence(SponsorshipEvidence(
        company_id=cid, company_name_raw="PgCo", source_type="DOL_LCA_DATA", fiscal_year=this_year,
        occupation_code="15-1252", occupation_title="Software Developers, Applications", worksite_state="CA",
    ))
    profile = refresh_employer_profile(cid)
    assert profile.historical_filing_count == 1
    assert profile.recent_occupation_families == ["software_engineering"]


def test_decision_versioning_on_postgres(pg_db):
    from app.sponsorship.decision import persist_decision

    d1 = persist_decision(1, "Engineer", "PgDecisionCo", "Join our backend team.", "CA")
    assert d1.decision_version == 1
    d2 = persist_decision(1, "Engineer", "PgDecisionCo", "We sponsor H-1B visas for this role.", "CA")
    assert d2.decision_version == 2


def test_idempotent_evidence_insert_on_postgres(pg_db):
    from app.sponsorship.datasets import get_or_create_dataset
    from app.sponsorship.evidence import SponsorshipEvidence, record_evidence_idempotent

    dataset = get_or_create_dataset("pg_test_dataset", dataset_version="v1")
    ev = SponsorshipEvidence(
        company_name_raw="DupePgCo", source_type="USCIS_EMPLOYER_DATA",
        dataset_id=dataset["id"], source_record_id="rec-1", fiscal_year=2024,
    )
    id1, created1 = record_evidence_idempotent(ev)
    id2, created2 = record_evidence_idempotent(ev)
    assert created1 is True
    assert created2 is False
    assert id1 == id2
