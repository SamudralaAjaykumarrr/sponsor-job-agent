"""Domain-seed acquisition pipeline (CLAUDE.md Phase 6 section 25).

Bulk input containing only `company_name` + `company_domain` -> bounded
careers discovery (app.registry.page_discovery.discover_career_links,
already built in Phase 4 but never wired into a bulk pipeline until now) ->
ATS detection -> tenant extraction -> handed to the existing conservative
upsert engine (app.registry.importers.process_row), which creates a
DISCOVERED/CANDIDATE portal -- verification (app.registry.verification)
still has to independently confirm it live before it can ever become
VERIFIED/ACTIVE, exactly like every other import path. No uncontrolled
crawling: discover_career_links already bounds pages/timeouts/redirects/
response size per domain (see its own docstring); this module bounds the
whole BATCH by processing one domain at a time, sequentially, never firing
concurrent requests at many companies' domains at once."""

import logging
from dataclasses import dataclass, field
from typing import Optional

from app.registry.importers import ImportSummary, RegistryCandidate, process_row
from app.registry.page_discovery import DiscoveryResult, discover_career_links

logger = logging.getLogger("registry.domain_seed")


@dataclass
class DomainSeedRowResult:
    company_name: str
    company_domain: str
    provider: str = ""
    tenant_identifier: str = ""
    careers_url: str = ""
    pages_fetched: int = 0
    portal_id: Optional[int] = None
    error: str = ""


@dataclass
class DomainSeedBatchResult:
    rows: list[DomainSeedRowResult] = field(default_factory=list)
    companies_discovered_ats: int = 0
    companies_no_match: int = 0
    rows_invalid: int = 0


def build_candidate_from_domain(
    company_name: str, company_domain: str, *, source: str = "domain_seed", source_url: str = "", row_number: int = 0,
) -> tuple[RegistryCandidate, DiscoveryResult]:
    discovery = discover_career_links(company_domain)
    if discovery.best_match and discovery.best_match.provider and discovery.best_match.tenant_identifier:
        candidate = RegistryCandidate(
            company_name=company_name, company_domain=company_domain,
            careers_url=discovery.best_match_url, provider=discovery.best_match.provider,
            tenant_identifier=discovery.best_match.tenant_identifier,
            source=source, source_url=source_url, row_number=row_number,
        )
    else:
        candidate = RegistryCandidate(
            company_name=company_name, company_domain=company_domain,
            source=source, source_url=source_url, row_number=row_number,
        )
    return candidate, discovery


def run_domain_seed_batch(
    rows: list[tuple[str, str]], *, source_name: str = "domain-seed", dry_run: bool = False,
) -> DomainSeedBatchResult:
    """Processes company_name/company_domain pairs ONE AT A TIME (never
    concurrently) -- each domain gets its own bounded discover_career_links()
    call, then process_row()'s existing idempotent upsert. Never fabricates
    a provider/tenant when discovery finds nothing: the row still becomes a
    bare Company (or DISCOVERED-with-no-portal) row, exactly like any other
    importer row with no usable ATS signal, for a human/later pass to
    revisit."""
    summary = ImportSummary(source_name=source_name, dry_run=dry_run)
    result = DomainSeedBatchResult()

    for i, (company_name, company_domain) in enumerate(rows, start=1):
        try:
            candidate, discovery = build_candidate_from_domain(
                company_name, company_domain, source=source_name, row_number=i,
            )
        except Exception as exc:  # noqa: BLE001 - one bad domain must never abort the batch
            logger.warning("domain seed discovery failed for '%s'", company_domain, exc_info=True)
            result.rows.append(DomainSeedRowResult(company_name=company_name, company_domain=company_domain, error=str(exc)))
            result.rows_invalid += 1
            continue

        row_result = DomainSeedRowResult(
            company_name=company_name, company_domain=company_domain, provider=candidate.provider,
            tenant_identifier=candidate.tenant_identifier, careers_url=candidate.careers_url,
            pages_fetched=discovery.pages_fetched,
        )
        if candidate.provider:
            result.companies_discovered_ats += 1
        else:
            result.companies_no_match += 1

        try:
            row_result.portal_id = process_row(candidate, source_name, summary, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 - one bad row must never abort the batch
            logger.warning("domain seed process_row failed for '%s'", company_domain, exc_info=True)
            row_result.error = str(exc)
            result.rows_invalid += 1

        result.rows.append(row_result)

    return result
