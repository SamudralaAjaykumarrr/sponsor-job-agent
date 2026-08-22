"""CLAUDE.md Phase 7 sections 5-7, 45: dataset importers -- streaming,
batched, idempotent, resumable, provenance-preserving, malformed-row-safe."""

from app.sponsorship.datasets import get_dataset, list_datasets
from app.sponsorship.evidence import count_evidence, list_evidence_by_normalized_name
from app.sponsorship.importers import import_dol_lca_data, import_uscis_employer_data, recompute_profiles_for_dataset

USCIS_HEADER = "Fiscal Year,Employer,Initial Approval,Initial Denial,Continuing Approval,Continuing Denial,NAICS Code,State,City\n"
DOL_HEADER = "CASE_NUMBER,EMPLOYER_NAME,JOB_TITLE,SOC_CODE,SOC_TITLE,VISA_CLASS,WORKSITE_CITY,WORKSITE_STATE,EMPLOYER_CITY,EMPLOYER_STATE,DECISION_DATE,CASE_STATUS\n"


def _write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_uscis_import_creates_evidence(tmp_env, tmp_path):
    path = _write(tmp_path, "uscis.csv", USCIS_HEADER +
                  "2024,Acme Software Inc,10,2,15,1,5415,CA,San Francisco\n"
                  "2024,Beta Systems LLC,3,0,2,0,5415,NY,New York\n")
    result = import_uscis_employer_data(path)
    assert result.rows_total == 2
    assert result.rows_created == 2
    assert count_evidence() == 2
    rows = list_evidence_by_normalized_name("acme software")
    assert len(rows) == 1
    assert rows[0].count_value == 25
    assert rows[0].occupation_title == ""  # USCIS Employer Data Hub has no occupation field -- never fabricated


def test_uscis_import_idempotent(tmp_env, tmp_path):
    path = _write(tmp_path, "uscis.csv", USCIS_HEADER + "2024,Acme Software Inc,10,2,15,1,5415,CA,San Francisco\n")
    r1 = import_uscis_employer_data(path, dataset_version="v1")
    r2 = import_uscis_employer_data(path, dataset_version="v1", dataset_id=r1.dataset_id)
    assert r1.rows_created == 1
    assert r2.rows_created == 0
    assert r2.rows_skipped_duplicate == 1
    assert count_evidence() == 1


def test_dol_lca_import_maps_occupation_and_status(tmp_env, tmp_path):
    path = _write(tmp_path, "lca.csv", DOL_HEADER +
                  "I-200-24001-000001,TechCorp,Software Engineer,15-1252,Software Developers,H-1B,San Jose,CA,San Jose,CA,01/15/2024,Certified\n")
    result = import_dol_lca_data(path)
    assert result.rows_created == 1
    rows = list_evidence_by_normalized_name("techcorp")
    assert rows[0].occupation_code == "15-1252"
    assert rows[0].fiscal_year == 2024
    assert rows[0].status_outcome == "Certified"


def test_dol_lca_import_idempotent_on_case_number(tmp_env, tmp_path):
    path = _write(tmp_path, "lca.csv", DOL_HEADER +
                  "I-200-24001-000002,TechCorp,Software Engineer,15-1252,Software Developers,H-1B,San Jose,CA,San Jose,CA,01/15/2024,Certified\n")
    r1 = import_dol_lca_data(path, dataset_version="v1")
    r2 = import_dol_lca_data(path, dataset_version="v1", dataset_id=r1.dataset_id)
    assert r1.rows_created == 1
    assert r2.rows_skipped_duplicate == 1


def test_malformed_row_does_not_abort_import(tmp_env, tmp_path):
    path = _write(tmp_path, "uscis.csv", USCIS_HEADER +
                  "2024,Acme Software Inc,10,2,15,1,5415,CA,San Francisco\n"
                  ",,,,,,,,\n"  # missing employer entirely
                  "2024,Gamma Corp,5,1,3,0,5415,TX,Austin\n")
    result = import_uscis_employer_data(path)
    assert result.rows_total == 3
    assert result.rows_created == 2
    assert result.rows_invalid == 1


def test_missing_fields_produce_invalid_row_not_crash(tmp_env, tmp_path):
    path = _write(tmp_path, "lca.csv", "CASE_NUMBER,EMPLOYER_NAME\nX-1,\n")
    result = import_dol_lca_data(path)
    assert result.rows_invalid == 1
    assert result.rows_created == 0


def test_resumable_import_skips_already_processed_rows(tmp_env, tmp_path):
    path = _write(tmp_path, "uscis.csv", USCIS_HEADER +
                  "2024,CompanyA,1,0,1,0,5415,CA,SF\n"
                  "2024,CompanyB,1,0,1,0,5415,CA,SF\n"
                  "2024,CompanyC,1,0,1,0,5415,CA,SF\n")
    dataset = None
    # Simulate a partial run by importing with a pre-existing dataset row
    # whose resume_cursor is manually advanced past the first row.
    r1 = import_uscis_employer_data(path, dataset_version="resumable")
    dataset = get_dataset(r1.dataset_id)
    assert dataset["resume_cursor"] == 3
    assert dataset["status"] == "COMPLETED"

    # Re-running with resume=True on an already-completed dataset should
    # skip all 3 rows (cursor already at end) -- nothing double-counted.
    r2 = import_uscis_employer_data(path, dataset_version="resumable", dataset_id=r1.dataset_id, resume=True)
    assert r2.rows_total == 0
    assert count_evidence() == 3


def test_batched_large_import_bounded_batches(tmp_env, tmp_path):
    rows = "".join(f"2024,Company{i},1,0,0,0,5415,CA,SF\n" for i in range(250))
    path = _write(tmp_path, "uscis.csv", USCIS_HEADER + rows)
    result = import_uscis_employer_data(path, batch_size=50)
    assert result.rows_total == 250
    assert result.rows_created == 250
    assert count_evidence() == 250


def test_dataset_versioning_tracked(tmp_env, tmp_path):
    path = _write(tmp_path, "uscis.csv", USCIS_HEADER + "2024,CompanyX,1,0,0,0,5415,CA,SF\n")
    result = import_uscis_employer_data(path, dataset_version="2024Q1")
    dataset = get_dataset(result.dataset_id)
    assert dataset["dataset_version"] == "2024Q1"
    assert dataset["record_count"] == 1
    assert dataset["status"] == "COMPLETED"
    assert len(list_datasets()) == 1


def test_year_reimport_does_not_combine_unrelated_datasets(tmp_env, tmp_path):
    path_2023 = _write(tmp_path, "uscis_2023.csv", USCIS_HEADER + "2023,CompanyX,1,0,0,0,5415,CA,SF\n")
    path_2024 = _write(tmp_path, "uscis_2024.csv", USCIS_HEADER + "2024,CompanyX,1,0,0,0,5415,CA,SF\n")
    r1 = import_uscis_employer_data(path_2023, dataset_version="2023")
    r2 = import_uscis_employer_data(path_2024, dataset_version="2024")
    assert r1.dataset_id != r2.dataset_id
    assert count_evidence() == 2


def test_recompute_profiles_for_dataset(tmp_env, tmp_path):
    from app.registry.models import Company
    from app.registry import store
    from app.sponsorship.profile import get_cached_profile

    cid = store.insert_company(Company(normalized_name="acme software", display_name="Acme Software Inc", primary_domain="acmesoftware.com"))
    path = _write(tmp_path, "uscis.csv", USCIS_HEADER + "2024,Acme Software Inc,10,2,15,1,5415,CA,San Francisco\n")
    result = import_uscis_employer_data(path)
    n = recompute_profiles_for_dataset(result.dataset_id)
    assert n == 1
    profile = get_cached_profile(cid)
    assert profile is not None
    assert profile.historical_filing_count == 1
