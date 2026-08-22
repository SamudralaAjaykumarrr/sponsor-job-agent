from app.sponsorship.evidence import (
    SponsorshipEvidence,
    bulk_record_evidence_idempotent,
    count_evidence,
    list_evidence_by_normalized_name,
    list_evidence_for_company,
    list_unresolved_evidence,
    record_evidence,
    record_evidence_idempotent,
)
from app.sponsorship.schema import SourceQuality, SourceType


def test_record_evidence_derives_normalized_name_and_quality(tmp_env):
    eid = record_evidence(SponsorshipEvidence(
        company_name_raw="Acme Widgets, Inc.", source_type=SourceType.DOL_LCA_DATA.value,
        fiscal_year=2024,
    ))
    rows = list_evidence_by_normalized_name("acme widgets")
    assert len(rows) == 1
    assert rows[0].id == eid
    assert rows[0].source_quality == SourceQuality.PRIMARY_GOVERNMENT.value


def test_snippet_is_bounded(tmp_env):
    long_text = "x" * 5000
    ev = SponsorshipEvidence(company_name_raw="Acme", snippet=long_text)
    assert len(ev.snippet) == 500


def test_no_beneficiary_pii_fields_exist():
    """CLAUDE.md Phase 7 section 37 -- the model must never carry a
    worker/beneficiary name field."""
    fields = set(SponsorshipEvidence.model_fields.keys())
    for forbidden in ("beneficiary_name", "worker_name", "employee_name", "ssn", "date_of_birth"):
        assert forbidden not in fields


def test_idempotent_insert_by_dataset_and_source_record_id(tmp_env):
    from app.sponsorship.datasets import get_or_create_dataset

    dataset = get_or_create_dataset("test_dataset", dataset_version="v1")
    ev = SponsorshipEvidence(
        company_name_raw="DupeCo", source_type=SourceType.USCIS_EMPLOYER_DATA.value,
        dataset_id=dataset["id"], source_record_id="rec-1", fiscal_year=2024,
    )
    id1, created1 = record_evidence_idempotent(ev)
    id2, created2 = record_evidence_idempotent(ev)
    assert created1 is True
    assert created2 is False
    assert id1 == id2
    assert count_evidence() == 1


def test_bulk_record_evidence_idempotent(tmp_env):
    from app.sponsorship.datasets import get_or_create_dataset

    dataset = get_or_create_dataset("bulk_test", dataset_version="v1")
    rows = [
        SponsorshipEvidence(company_name_raw="BulkCo", source_type=SourceType.DOL_LCA_DATA.value,
                             dataset_id=dataset["id"], source_record_id=f"rec-{i}", fiscal_year=2024)
        for i in range(5)
    ]
    created, skipped = bulk_record_evidence_idempotent(rows)
    assert created == 5
    assert skipped == 0
    created2, skipped2 = bulk_record_evidence_idempotent(rows)
    assert created2 == 0
    assert skipped2 == 5
    assert count_evidence() == 5


def test_unresolved_evidence_and_attach(tmp_env):
    from app.registry.models import Company
    from app.registry import store
    from app.sponsorship.evidence import attach_company

    eid = record_evidence(SponsorshipEvidence(company_name_raw="Unresolved Co", source_type=SourceType.DOL_LCA_DATA.value))
    assert len(list_unresolved_evidence()) == 1

    cid = store.insert_company(Company(normalized_name="unresolved co", display_name="Unresolved Co"))
    attach_company(eid, cid)
    assert len(list_unresolved_evidence()) == 0
    assert len(list_evidence_for_company(cid)) == 1
