"""Government sponsorship-dataset importers (CLAUDE.md Phase 7 sections 5-7).

Two supported, documented source formats (see docs/sponsorship-data-import.md
for the exact expected columns):

  USCIS_EMPLOYER_DATA: the public USCIS H-1B Employer Data Hub CSV -- one
    aggregate row per employer per fiscal year (approval/denial COUNTS, no
    job title/occupation -- USCIS's public hub genuinely doesn't publish
    that; never fabricated here).

  DOL_LCA_DATA: the public DOL Office of Foreign Labor Certification LCA
    disclosure CSV -- one row per Labor Condition Application, with job
    title/SOC occupation/worksite detail.

Both importers stream the CSV (never load the whole file into memory),
process in bounded batches, are idempotent (safe to re-run the identical
file), and are resumable via `sponsorship_datasets.resume_cursor` (a crash
partway through can be resumed by re-invoking with resume=True, which skips
already-processed rows before continuing). Only public, unauthenticated,
already-downloaded files are read here -- this module never performs a live
network download itself (see docs/sponsorship-data-import.md for why)."""

import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from app.sponsorship import datasets as datasets_mod
from app.sponsorship.evidence import SponsorshipEvidence, bulk_record_evidence_idempotent, compute_fingerprint
from app.sponsorship.identity import resolve_company
from app.sponsorship.schema import DatasetStatus, SourceType

DEFAULT_BATCH_SIZE = 1000


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ImportResult:
    dataset_id: int
    dataset_name: str
    rows_total: int = 0
    rows_created: int = 0
    rows_skipped_duplicate: int = 0
    rows_invalid: int = 0
    companies_matched: int = 0
    companies_ambiguous: int = 0
    companies_unmatched: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "dataset_id": self.dataset_id, "dataset_name": self.dataset_name, "rows_total": self.rows_total,
            "rows_created": self.rows_created, "rows_skipped_duplicate": self.rows_skipped_duplicate,
            "rows_invalid": self.rows_invalid, "companies_matched": self.companies_matched,
            "companies_ambiguous": self.companies_ambiguous, "companies_unmatched": self.companies_unmatched,
            "errors": self.errors,
        }


def _get(row: dict, *keys: str) -> str:
    for k in keys:
        if k in row and row[k] is not None:
            v = str(row[k]).strip()
            if v:
                return v
    return ""


def _to_int(value: str) -> Optional[int]:
    value = (value or "").strip().replace(",", "")
    if not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def _stream_csv_rows(path: Path, skip_rows: int) -> Iterator[tuple[int, dict]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            if i < skip_rows:
                continue
            yield i, row


def _uscis_fiscal_year_from_row(row: dict) -> Optional[int]:
    return _to_int(_get(row, "Fiscal Year", "fiscal_year", "FY"))


def import_uscis_employer_data(
    path: str | Path, *, dataset_version: str = "", source_url: str = "",
    batch_size: int = DEFAULT_BATCH_SIZE, resume: bool = False, dataset_id: Optional[int] = None,
) -> ImportResult:
    """USCIS H-1B Employer Data Hub CSV. Expected columns (case-sensitive,
    matching the public download): Fiscal Year, Employer, Initial Approval,
    Initial Denial, Continuing Approval, Continuing Denial, NAICS Code,
    State, City. No job-title/occupation column exists in this dataset --
    occupation_code/occupation_title are always left blank for these rows
    (never fabricated)."""
    path = Path(path)
    return _run_import(
        path, source_type=SourceType.USCIS_EMPLOYER_DATA, dataset_name="uscis_h1b_employer_data_hub",
        dataset_version=dataset_version, source_url=source_url, row_mapper=_map_uscis_row,
        batch_size=batch_size, resume=resume, dataset_id=dataset_id,
    )


def import_dol_lca_data(
    path: str | Path, *, dataset_version: str = "", source_url: str = "",
    batch_size: int = DEFAULT_BATCH_SIZE, resume: bool = False, dataset_id: Optional[int] = None,
) -> ImportResult:
    """DOL OFLC LCA disclosure CSV. Expected columns (matching the public
    quarterly disclosure download): CASE_NUMBER, EMPLOYER_NAME, JOB_TITLE,
    SOC_CODE, SOC_TITLE, VISA_CLASS, WORKSITE_CITY, WORKSITE_STATE,
    EMPLOYER_CITY, EMPLOYER_STATE, DECISION_DATE, CASE_STATUS,
    FULL_TIME_POSITION."""
    path = Path(path)
    return _run_import(
        path, source_type=SourceType.DOL_LCA_DATA, dataset_name="dol_oflc_lca_disclosure",
        dataset_version=dataset_version, source_url=source_url, row_mapper=_map_dol_lca_row,
        batch_size=batch_size, resume=resume, dataset_id=dataset_id,
    )


def _map_uscis_row(row: dict, dataset_id: int) -> Optional[SponsorshipEvidence]:
    employer = _get(row, "Employer", "employer", "Petitioner Name")
    if not employer:
        return None
    fiscal_year = _uscis_fiscal_year_from_row(row)
    state = _get(row, "State", "Petitioner State")
    city = _get(row, "City", "Petitioner City")
    initial_approval = _to_int(_get(row, "Initial Approval", "Initial Approvals")) or 0
    continuing_approval = _to_int(_get(row, "Continuing Approval", "Continuing Approvals")) or 0
    count_value = initial_approval + continuing_approval
    record_id = compute_fingerprint("uscis", employer, str(fiscal_year), state, city)
    return SponsorshipEvidence(
        company_name_raw=employer, source="USCIS_EMPLOYER_DATA_HUB", source_type=SourceType.USCIS_EMPLOYER_DATA.value,
        dataset_id=dataset_id, source_record_id=record_id, fiscal_year=fiscal_year,
        petition_type="H-1B", visa_class="H-1B", employer_city=city, employer_state=state,
        location=f"{city}, {state}".strip(", "), count_value=count_value, confidence=90,
        notes="USCIS Employer Data Hub: aggregate approval counts, no job-title/occupation field in this source",
    )


def _map_dol_lca_row(row: dict, dataset_id: int) -> Optional[SponsorshipEvidence]:
    employer = _get(row, "EMPLOYER_NAME", "Employer Name", "employer_name")
    if not employer:
        return None
    case_number = _get(row, "CASE_NUMBER", "Case Number")
    fiscal_year = None
    decision_date = _get(row, "DECISION_DATE", "Decision Date")
    if decision_date:
        for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
            try:
                fiscal_year = datetime.strptime(decision_date, fmt).year
                break
            except ValueError:
                continue
    record_id = case_number or compute_fingerprint(
        "dol_lca", employer, decision_date, _get(row, "JOB_TITLE", "Job Title"),
    )
    return SponsorshipEvidence(
        company_name_raw=employer, source="DOL_OFLC_LCA_DISCLOSURE", source_type=SourceType.DOL_LCA_DATA.value,
        dataset_id=dataset_id, source_record_id=record_id, fiscal_year=fiscal_year, filing_date=decision_date or None,
        petition_type="LCA", visa_class=_get(row, "VISA_CLASS", "Visa Class") or "H-1B",
        job_title=_get(row, "JOB_TITLE", "Job Title"), occupation_code=_get(row, "SOC_CODE", "SOC Code"),
        occupation_title=_get(row, "SOC_TITLE", "SOC Title"),
        worksite_city=_get(row, "WORKSITE_CITY", "Worksite City"), worksite_state=_get(row, "WORKSITE_STATE", "Worksite State"),
        employer_city=_get(row, "EMPLOYER_CITY", "Employer City"), employer_state=_get(row, "EMPLOYER_STATE", "Employer State"),
        status_outcome=_get(row, "CASE_STATUS", "Case Status"), confidence=90,
    )


def _run_import(
    path: Path, *, source_type: SourceType, dataset_name: str, dataset_version: str, source_url: str,
    row_mapper, batch_size: int, resume: bool, dataset_id: Optional[int],
) -> ImportResult:
    if dataset_id is not None:
        dataset = datasets_mod.get_dataset(dataset_id)
        if dataset is None:
            raise ValueError(f"no such dataset id={dataset_id}")
    else:
        dataset = datasets_mod.get_or_create_dataset(
            dataset_name, dataset_version=dataset_version, source_url=source_url,
        )
    dataset_id = dataset["id"]
    skip_rows = dataset.get("resume_cursor", 0) if resume else 0

    datasets_mod.update_dataset(dataset_id, status=DatasetStatus.IMPORTING.value)

    result = ImportResult(dataset_id=dataset_id, dataset_name=dataset_name)
    batch: list[SponsorshipEvidence] = []
    matched_companies: set[int] = set()
    ambiguous = unmatched = 0
    last_index = skip_rows - 1

    def flush():
        nonlocal ambiguous, unmatched
        if not batch:
            return
        created, skipped = bulk_record_evidence_idempotent(batch)
        result.rows_created += created
        result.rows_skipped_duplicate += skipped
        batch.clear()

    try:
        for index, raw_row in _stream_csv_rows(path, skip_rows):
            last_index = index
            result.rows_total += 1
            try:
                evidence = row_mapper(raw_row, dataset_id)
            except Exception as exc:  # noqa: BLE001 -- one bad row must never abort the import
                result.rows_invalid += 1
                result.errors.append(f"row {index}: {exc}")
                continue
            if evidence is None:
                result.rows_invalid += 1
                result.errors.append(f"row {index}: missing employer name")
                continue

            match = resolve_company(evidence.company_name_raw, evidence.company_domain)
            if match.company_id is not None:
                evidence.company_id = match.company_id
                matched_companies.add(match.company_id)
            elif match.matched_via == "ambiguous":
                ambiguous += 1
            else:
                unmatched += 1

            batch.append(evidence)
            if len(batch) >= batch_size:
                flush()
                datasets_mod.update_dataset(dataset_id, resume_cursor=last_index + 1)
        flush()
        datasets_mod.update_dataset(dataset_id, resume_cursor=last_index + 1)
        datasets_mod.mark_completed(dataset_id, record_count=result.rows_created)
    except Exception as exc:  # noqa: BLE001 -- record the failure, never crash the caller silently
        flush()
        datasets_mod.mark_failed(dataset_id, str(exc))
        result.errors.append(f"import aborted: {exc}")

    result.companies_matched = len(matched_companies)
    result.companies_ambiguous = ambiguous
    result.companies_unmatched = unmatched
    return result


def recompute_profiles_for_dataset(dataset_id: int) -> int:
    """CLAUDE.md Phase 7 section 6/53: after a dataset import, recompute the
    cached employer profile for every DISTINCT company it touched -- never
    for the whole registry."""
    from app.db import db_session
    from app.sponsorship.profile import refresh_employer_profile

    with db_session() as conn:
        rows = conn.execute(
            "SELECT DISTINCT company_id FROM employer_sponsorship_evidence "
            "WHERE dataset_id = ? AND company_id IS NOT NULL",
            (dataset_id,),
        ).fetchall()
    for r in rows:
        refresh_employer_profile(r["company_id"])
    return len(rows)
