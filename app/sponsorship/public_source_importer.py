"""Public-web LCA-aggregator snapshot importer (Sponsorship Intelligence
Coverage V1). A THIRD supported evidence source alongside the two government
CSV importers in app/sponsorship/importers.py.

Why this exists: USCIS's H-1B Employer Data Hub (SourceType.USCIS_EMPLOYER_DATA)
is aggregate-only and genuinely has no job-title/occupation field. Without
per-record occupation data, app.sponsorship.profile can never set
`recent_technical=True`, which caps every employer's historical_strength at
SOME and blocks app.sponsorship.decision's UNKNOWN -> LIKELY_SPONSOR upgrade
path entirely, no matter how much USCIS aggregate history exists. DOL's own
OFLC LCA disclosure files (SourceType.DOL_LCA_DATA) carry the needed job-title
detail but were not reachable over the network available to this importer
(dol.gov / foreignlaborcert.doleta.gov returned 403/503 to every fetch
attempted while building this feature).

h1bdata.info is a long-running, public, unauthenticated, robots.txt-open
mirror of the same DOL LCA disclosure records (per-record job title, salary,
worksite, submit/start date, and the real DOL case number), searchable by
exact employer name. Because this is a THIRD-PARTY re-publication rather than
a government file we downloaded ourselves, every row here is recorded at
SourceType.OTHER_REPUTABLE_PUBLIC_SOURCE / SourceQuality.SECONDARY_REPUTABLE
(weight 0.3) -- never PRIMARY_GOVERNMENT -- regardless of how authentic the
underlying case numbers look. This is a deliberate, conservative provenance
choice, not an oversight.

Same operator-driven contract as the two CSV importers: this module NEVER
performs a live network fetch. An operator saves a company's h1bdata.info
search-results page (View Source / curl) to a local .html file and this
importer parses that already-downloaded snapshot. Streaming a >1MB HTML page
through a DOM parser is unnecessary at this data's realistic per-employer
scale (low thousands of rows); a bounded, deterministic regex-based row
extraction is used instead, matching the exact fixed row shape h1bdata.info's
results table has always used:

    <tr><td><a ...>EMPLOYER</a></td><td><a ...>JOB TITLE</a></td>
        <td><a href="details.php?id=CASE_ID" ...>SALARY</a></td>
        <td><a ...>CITY, ST</a></td>
        <td class="d-sm-none">MM/DD/YYYY</td><td class="d-sm-none">MM/DD/YYYY</td></tr>

Anti-contamination guard (the one bug this design must never regress): a
substring/OR-token search on this site can return an UNRELATED company whose
name happens to share a word (verified live while building this feature --
searching "GITLAB" returns real LCA rows for "GITLAB FOUNDATION", a distinct
nonprofit, not the GitLab Inc. software company at all). `expected_employer`
is therefore REQUIRED, and any row whose employer-column text does not
normalize-equal it is dropped into `rows_rejected_employer_mismatch`, never
silently imported under the caller's intended company."""

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.registry.normalize import normalize_company_name
from app.sponsorship import datasets as datasets_mod
from app.sponsorship.evidence import SponsorshipEvidence, bulk_record_evidence_idempotent, compute_fingerprint
from app.sponsorship.identity import resolve_company
from app.sponsorship.schema import DatasetStatus, SourceType

DEFAULT_DATASET_NAME = "h1bdata_info_lca_snapshot"
DEFAULT_SOURCE_URL = "https://h1bdata.info"

_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_ANCHOR_TEXT_RE = re.compile(r">([^<]*)</a>", re.S)
_CASE_ID_RE = re.compile(r"details\.php\?id=([A-Za-z0-9\-]+)")
_DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")


@dataclass
class PublicSourceImportResult:
    dataset_id: int
    dataset_name: str
    employer_query: str
    rows_total: int = 0
    rows_created: int = 0
    rows_skipped_duplicate: int = 0
    rows_invalid: int = 0
    rows_rejected_employer_mismatch: int = 0
    rejected_employer_names: list[str] = field(default_factory=list)
    company_id: Optional[int] = None
    company_match_via: str = ""

    def as_dict(self) -> dict:
        return {
            "dataset_id": self.dataset_id, "dataset_name": self.dataset_name,
            "employer_query": self.employer_query, "rows_total": self.rows_total,
            "rows_created": self.rows_created, "rows_skipped_duplicate": self.rows_skipped_duplicate,
            "rows_invalid": self.rows_invalid,
            "rows_rejected_employer_mismatch": self.rows_rejected_employer_mismatch,
            "rejected_employer_names": self.rejected_employer_names,
            "company_id": self.company_id, "company_match_via": self.company_match_via,
        }


def _anchor_text(cell_html: str) -> str:
    m = _ANCHOR_TEXT_RE.search(cell_html)
    text = m.group(1) if m else re.sub(r"<[^>]+>", "", cell_html)
    return text.strip()


def _parse_date(text: str) -> Optional[str]:
    m = _DATE_RE.search(text or "")
    if not m:
        return None
    mm, dd, yyyy = m.groups()
    try:
        return datetime(int(yyyy), int(mm), int(dd)).date().isoformat()
    except ValueError:
        return None


def _parse_location(text: str) -> tuple[str, str]:
    if "," not in text:
        return text.strip(), ""
    city, _, state = text.rpartition(",")
    return city.strip(), state.strip()


@dataclass
class _ParsedRow:
    employer: str
    job_title: str
    case_id: str
    city: str
    state: str
    submit_date: Optional[str]
    start_date: Optional[str]


def _parse_snapshot_rows(html: str) -> list[_ParsedRow]:
    """Bounded, deterministic parse of one h1bdata.info results page. Never
    fabricates a field that isn't literally present in a row -- a row with
    fewer than 4 plain <td> cells is skipped entirely (never partially
    guessed)."""
    parsed: list[_ParsedRow] = []
    for row_html in _ROW_RE.findall(html):
        cells = _TD_RE.findall(row_html)
        if len(cells) < 4:
            continue
        employer = _anchor_text(cells[0])
        job_title = _anchor_text(cells[1])
        case_match = _CASE_ID_RE.search(cells[2])
        case_id = case_match.group(1) if case_match else ""
        location_text = _anchor_text(cells[3])
        city, state = _parse_location(location_text)
        submit_date = _parse_date(cells[4]) if len(cells) > 4 else None
        start_date = _parse_date(cells[5]) if len(cells) > 5 else None
        if not employer or not job_title:
            continue
        parsed.append(_ParsedRow(
            employer=employer, job_title=job_title, case_id=case_id,
            city=city, state=state, submit_date=submit_date, start_date=start_date,
        ))
    return parsed


def import_h1bdata_snapshot(
    path: str | Path, expected_employer: str, *, dataset_version: str = "",
    source_url: str = DEFAULT_SOURCE_URL, dataset_id: Optional[int] = None,
) -> PublicSourceImportResult:
    """Imports one already-downloaded h1bdata.info employer-search snapshot.

    `expected_employer` (the exact legal-entity name the operator searched
    for, e.g. "STRIPE INC") is REQUIRED and enforced per-row -- see module
    docstring's anti-contamination guard. Comparison is via
    app.registry.normalize.normalize_company_name so suffix/punctuation/case
    differences never cause a false rejection, but a genuinely different
    company name (e.g. "GITLAB FOUNDATION" when searching for "GITLAB") is
    always rejected, never imported."""
    path = Path(path)
    expected_normalized = normalize_company_name(expected_employer)
    if not expected_normalized:
        raise ValueError("expected_employer must be non-empty")

    html = path.read_text(encoding="utf-8", errors="replace")
    rows = _parse_snapshot_rows(html)

    if dataset_id is not None:
        dataset = datasets_mod.get_dataset(dataset_id)
        if dataset is None:
            raise ValueError(f"no such dataset id={dataset_id}")
    else:
        dataset = datasets_mod.get_or_create_dataset(
            DEFAULT_DATASET_NAME, dataset_version=dataset_version or expected_normalized,
            source_url=source_url,
        )
    dataset_id = dataset["id"]
    datasets_mod.update_dataset(dataset_id, status=DatasetStatus.IMPORTING.value)

    result = PublicSourceImportResult(
        dataset_id=dataset_id, dataset_name=DEFAULT_DATASET_NAME, employer_query=expected_employer,
    )
    batch: list[SponsorshipEvidence] = []
    rejected_names: set[str] = set()
    matched_company_id: Optional[int] = None
    matched_via = ""

    for row in rows:
        result.rows_total += 1
        if normalize_company_name(row.employer) != expected_normalized:
            result.rows_rejected_employer_mismatch += 1
            rejected_names.add(row.employer)
            continue

        fiscal_year = None
        filing_date = row.submit_date or row.start_date
        if filing_date:
            fiscal_year = int(filing_date[:4])

        record_id = row.case_id or compute_fingerprint(
            "h1bdata_info", row.employer, row.job_title, filing_date or "", row.city, row.state,
        )

        evidence = SponsorshipEvidence(
            company_name_raw=row.employer,
            source="H1BDATA_INFO_LCA_MIRROR",
            source_type=SourceType.OTHER_REPUTABLE_PUBLIC_SOURCE.value,
            source_url=source_url,
            source_record_id=record_id,
            dataset_id=dataset_id,
            fiscal_year=fiscal_year,
            filing_date=filing_date,
            petition_type="LCA",
            visa_class="H-1B",
            job_title=row.job_title,
            # h1bdata.info exposes the LCA-filed job title only, never a SOC
            # code/title -- occupation_title mirrors job_title (the same text
            # a real DOL_LCA_DATA row would put in SOC_TITLE is not available
            # here and is never guessed) so app.sponsorship.profile's
            # occupation-family aggregation has real title text to work with.
            occupation_title=row.job_title,
            worksite_city=row.city,
            worksite_state=row.state,
            confidence=60,
            notes=(
                "Third-party public mirror of DOL LCA disclosure data (h1bdata.info), not a "
                "direct government file download -- recorded as OTHER_REPUTABLE_PUBLIC_SOURCE/"
                "SECONDARY_REPUTABLE, never PRIMARY_GOVERNMENT. occupation_title is the raw "
                "employer-filed job title text; no SOC code is available from this source."
            ),
        )

        if matched_company_id is None:
            match = resolve_company(evidence.company_name_raw)
            if match.company_id is not None:
                evidence.company_id = match.company_id
                matched_company_id = match.company_id
                matched_via = match.matched_via
        else:
            evidence.company_id = matched_company_id

        batch.append(evidence)

    if batch:
        created, skipped = bulk_record_evidence_idempotent(batch)
        result.rows_created = created
        result.rows_skipped_duplicate = skipped

    result.rejected_employer_names = sorted(rejected_names)
    result.company_id = matched_company_id
    result.company_match_via = matched_via

    datasets_mod.mark_completed(dataset_id, record_count=(datasets_mod.get_dataset(dataset_id) or {}).get("record_count", 0) + result.rows_created)
    return result
