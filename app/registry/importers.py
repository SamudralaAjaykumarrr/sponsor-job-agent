"""Bulk registry import: RegistrySource interface + CSV/JSON/JSONL readers +
the idempotent upsert engine that turns RegistryCandidate rows into
Company/CareerPortal/Provenance records. Every source produces CANDIDATES,
never automatically-trusted records -- see CLAUDE.md Phase 4 sections 4, 14, 15.

Import is deliberately conservative:
  - A row with no usable identity information at all is INVALID, not silently
    dropped -- it's reported in the summary.
  - A row with only a company name (no provider/tenant/URL) still creates a
    Company row, with no portal.
  - A row with provider+tenant is deduped on (provider, tenant_identifier).
  - A row with only a careers_url (no provider) is deduped on its
    canonicalized URL, and left as DISCOVERED -- never guessing a provider.
  - Re-importing the identical dataset must not create duplicate rows
    (verified by tests/test_registry_import.py's idempotency tests)."""

import csv
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

from app.providers.capabilities import SupportLevel
from app.providers.detector import detect_provider
from app.registry import store
from app.registry.quality import score_portal
from app.registry.models import (
    CareerPortal,
    Company,
    DiscoveryStatus,
    PortalStatus,
    RegistryProvenance,
)
from app.registry.normalize import normalize_company_name, normalize_domain
from app.registry.url_canon import canonicalize_portal_url, is_valid_http_url

logger = logging.getLogger("registry.importers")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RegistryCandidate:
    company_name: str = ""
    company_domain: str = ""
    careers_url: str = ""
    provider: str = ""
    tenant_identifier: str = ""
    country: str = ""
    source: str = ""
    source_url: str = ""
    row_number: int = 0


@dataclass
class ImportSummary:
    source_name: str
    dry_run: bool
    rows_total: int = 0
    rows_created: int = 0
    rows_updated: int = 0
    rows_skipped: int = 0
    rows_invalid: int = 0
    companies_created: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "source_name": self.source_name, "dry_run": self.dry_run, "rows_total": self.rows_total,
            "rows_created": self.rows_created, "rows_updated": self.rows_updated,
            "rows_skipped": self.rows_skipped, "rows_invalid": self.rows_invalid,
            "companies_created": self.companies_created, "errors": self.errors,
        }


# --- RegistrySource readers -------------------------------------------------

_FIELD_ALIASES = {
    "company_name": ("company_name", "company", "name"),
    "company_domain": ("company_domain", "domain"),
    "careers_url": ("careers_url", "careers_page", "url"),
    "provider": ("provider", "ats", "ats_provider"),
    "tenant_identifier": ("tenant_identifier", "tenant", "board_token", "slug"),
    "country": ("country",),
    "source": ("source", "source_name"),
    "source_url": ("source_url",),
}


def _get_field(row: dict, canonical: str) -> str:
    for alias in _FIELD_ALIASES[canonical]:
        if alias in row and row[alias] is not None:
            return str(row[alias]).strip()
    return ""


def _row_to_candidate(row: dict, row_number: int) -> RegistryCandidate:
    return RegistryCandidate(
        company_name=_get_field(row, "company_name"),
        company_domain=_get_field(row, "company_domain"),
        careers_url=_get_field(row, "careers_url"),
        provider=_get_field(row, "provider").lower(),
        tenant_identifier=_get_field(row, "tenant_identifier"),
        country=_get_field(row, "country"),
        source=_get_field(row, "source"),
        source_url=_get_field(row, "source_url"),
        row_number=row_number,
    )


def read_csv(path: Path) -> Iterator[RegistryCandidate]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, start=2):  # header is row 1
            yield _row_to_candidate(row, i)


def read_json(path: Path) -> Iterator[RegistryCandidate]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("companies") or data.get("records") or data.get("items") or []
    for i, row in enumerate(data, start=1):
        yield _row_to_candidate(row, i)


def read_jsonl(path: Path) -> Iterator[RegistryCandidate]:
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            yield _row_to_candidate(json.loads(line), i)


_READERS = {".csv": read_csv, ".json": read_json, ".jsonl": read_jsonl, ".ndjson": read_jsonl}


def read_candidates(path: str | Path) -> Iterator[RegistryCandidate]:
    path = Path(path)
    reader = _READERS.get(path.suffix.lower())
    if reader is None:
        raise ValueError(f"unsupported import format '{path.suffix}' -- expected one of {sorted(_READERS)}")
    return reader(path)


# --- Import engine -----------------------------------------------------------

def _upsert_company(candidate: RegistryCandidate, summary: ImportSummary, dry_run: bool) -> Optional[Company]:
    normalized_name = normalize_company_name(candidate.company_name)
    normalized_domain = normalize_domain(candidate.company_domain) if candidate.company_domain else ""
    if not normalized_name:
        return None

    existing = store.get_company_by_identity(normalized_name, normalized_domain)
    if existing is None and not normalized_domain:
        existing = store.get_company_by_name_only(normalized_name)

    if existing is not None:
        return existing

    if dry_run:
        # Synthetic in-memory placeholder so the rest of the row can still be
        # validated/reported without writing anything.
        return Company(id=-1, normalized_name=normalized_name, display_name=candidate.company_name,
                        primary_domain=normalized_domain, country=candidate.country)

    company_id = store.insert_company(Company(
        normalized_name=normalized_name, display_name=candidate.company_name,
        primary_domain=normalized_domain, country=candidate.country,
    ))
    summary.companies_created += 1
    return store.get_company(company_id)


def _find_existing_portal(candidate: RegistryCandidate, canonical: str) -> Optional[CareerPortal]:
    if candidate.provider and candidate.tenant_identifier:
        existing = store.get_portal_by_provider_tenant(candidate.provider, candidate.tenant_identifier)
        if existing is not None:
            return existing
    if canonical:
        return store.get_portal_by_canonical_url(canonical)
    return None


def _process_row(candidate: RegistryCandidate, source_name: str, summary: ImportSummary, dry_run: bool) -> None:
    if not candidate.company_name.strip():
        summary.rows_invalid += 1
        summary.errors.append(f"row {candidate.row_number}: missing company_name")
        return

    if candidate.careers_url and not is_valid_http_url(candidate.careers_url):
        summary.rows_invalid += 1
        summary.errors.append(f"row {candidate.row_number}: invalid careers_url '{candidate.careers_url}'")
        return

    company = _upsert_company(candidate, summary, dry_run)
    if company is None:
        summary.rows_invalid += 1
        summary.errors.append(f"row {candidate.row_number}: could not normalize company_name '{candidate.company_name}'")
        return

    provider = candidate.provider
    tenant = candidate.tenant_identifier
    careers_url = candidate.careers_url

    # Detect provider/tenant from the URL only when the row didn't already
    # supply them explicitly -- never override an explicitly supplied value,
    # and never claim higher confidence than the detector actually reports.
    detected = None
    if careers_url and not (provider and tenant):
        detected = detect_provider(careers_url)
        if detected.provider and not provider:
            provider = detected.provider
        if detected.tenant_identifier and not tenant:
            tenant = detected.tenant_identifier

    canonical = canonicalize_portal_url(careers_url) if careers_url else ""

    if not provider and not careers_url:
        # Company-only row -- no portal to create.
        summary.rows_skipped += 1
        return

    existing_portal = _find_existing_portal(candidate, canonical)

    if existing_portal is not None:
        if not dry_run:
            prov = RegistryProvenance(
                portal_id=existing_portal.id, company_id=company.id, source_type="bulk_import",
                source_name=candidate.source or source_name, source_url=candidate.source_url,
                evidence=f"re-observed in import row {candidate.row_number}",
                confidence=40,
            )
            store.upsert_provenance(prov)
        summary.rows_updated += 1
        return

    if dry_run:
        summary.rows_created += 1
        return

    support_level = _support_level_for(provider)
    portal = CareerPortal(
        company_id=company.id,
        provider=provider,
        tenant_identifier=tenant,
        careers_url=careers_url,
        canonical_url=canonical,
        support_level=support_level,
        discovery_status=DiscoveryStatus.IMPORTED,
        verification_status=PortalStatus.CANDIDATE if (provider and tenant) else PortalStatus.DISCOVERED,
        notes="" if not detected else detected.evidence,
    )
    portal_id = store.insert_portal(portal)
    store.upsert_provenance(RegistryProvenance(
        portal_id=portal_id, company_id=company.id, source_type="bulk_import",
        source_name=candidate.source or source_name, source_url=candidate.source_url,
        evidence=f"imported from row {candidate.row_number}" + (f"; {detected.evidence}" if detected else ""),
        confidence=40,
    ))
    quality = score_portal(store.get_portal(portal_id), has_official_link_provenance=False)
    store.update_portal(portal_id, confidence=quality.score, confidence_reasons=quality.reasons)
    summary.rows_created += 1


def _support_level_for(provider: str) -> SupportLevel:
    from app.providers.registry import get_capabilities

    caps = get_capabilities(provider) if provider else None
    return caps.support_level if caps else SupportLevel.UNSUPPORTED


def import_candidates(
    candidates: Iterable[RegistryCandidate], *, source_name: str, dry_run: bool = False, batch_size: int = 500,
) -> ImportSummary:
    """Processes candidates one at a time (each company/portal upsert is its
    own small transaction via app.db.db_session) -- `batch_size` only bounds
    how much is buffered in memory when driving from a generator, so a
    100k-row file streams through without ever holding the whole dataset."""
    summary = ImportSummary(source_name=source_name, dry_run=dry_run)
    batch: list[RegistryCandidate] = []

    def flush():
        for c in batch:
            summary.rows_total += 1
            try:
                _process_row(c, source_name, summary, dry_run)
            except Exception as exc:  # noqa: BLE001 - one bad row must never abort the whole import
                summary.rows_invalid += 1
                summary.errors.append(f"row {c.row_number}: {exc}")
                logger.warning("import row %s failed", c.row_number, exc_info=True)
        batch.clear()

    for candidate in candidates:
        batch.append(candidate)
        if len(batch) >= batch_size:
            flush()
    flush()
    return summary


def import_file(path: str | Path, *, source_name: Optional[str] = None, dry_run: bool = False, batch_size: int = 500) -> ImportSummary:
    path = Path(path)
    return import_candidates(read_candidates(path), source_name=source_name or path.name, dry_run=dry_run, batch_size=batch_size)
