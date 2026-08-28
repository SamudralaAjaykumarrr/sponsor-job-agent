import json
import logging
from datetime import datetime, timezone

from app import config
from app.discovery.dedup import canonicalize_url, fingerprint
from app.freshness.tracker import compute_age_minutes
from app.jobs_repo import (
    finalize_discovery_cycle,
    get_job_by_canonical_url,
    get_job_by_fingerprint,
    get_job_by_provider_external_id,
    insert_discovery_log,
    insert_job,
    record_provenance,
    start_discovery_cycle,
    touch_last_seen,
)
from app.matching.employment_type import is_full_time
from app.matching.geography import is_us_location
from app.models import ApplicationMode, ApplicationState, FreshnessSource, Job, SponsorshipStatus
from app.pipeline import analyze_job, generate_assist_outputs
from app.providers.base import RawJobPosting
from app.providers.registry import build_provider_for_tenant, get_enabled_providers
from app.registry.repo import list_due_for_poll, mark_poll_result

logger = logging.getLogger("agent.cycle")


def _pre_filter_reason(raw: RawJobPosting) -> str | None:
    """Discovery-time filters applied BEFORE a job is stored. Returns a skip
    reason, or None if the job should proceed. These are source-filtering
    concerns (country/employment-type/freshness-cutoff) distinct from the
    analysis-time gates in pipeline.analyze_job, which apply uniformly to
    manually-pasted jobs too."""
    if not is_full_time(raw.employment_type_raw, raw.description):
        return "not full-time"
    if not is_us_location(raw.location):
        return "not a US location"
    if raw.published_at:
        age_minutes = compute_age_minutes(raw.published_at, "")
        if age_minutes is not None and age_minutes / 1440.0 > config.FRESHNESS_MAX_DAYS:
            return f"older than freshness cutoff ({config.FRESHNESS_MAX_DAYS} days)"
    return None


def _raw_to_job(raw: RawJobPosting, canonical_url: str = "", correlation_id: str = "") -> Job:
    return Job(
        title=raw.title,
        company=raw.company,
        location=raw.location,
        description=raw.description,
        url=raw.url,
        source=raw.provider,
        provider=raw.provider,
        external_job_id=raw.external_job_id,
        employment_type=raw.employment_type_raw,
        salary_min=raw.salary_min,
        salary_max=raw.salary_max,
        dedup_fingerprint=fingerprint(raw.company, raw.title, raw.location),
        company_identifier=raw.company_identifier or "",
        city=raw.city or "",
        state=raw.state or "",
        country=raw.country or "",
        remote_status=raw.remote_status or "",
        department=raw.department or "",
        team=raw.team or "",
        office=raw.office or "",
        source_url=raw.source_url or raw.url or "",
        canonical_url=canonical_url,
        salary_currency=raw.salary_currency or "",
        salary_period=raw.salary_period or "",
        provider_metadata=json.dumps(raw.provider_metadata or {}, default=str),
        published_at=raw.published_at,
        freshness_source=FreshnessSource.PUBLISHED_AT if raw.published_at else FreshnessSource.FIRST_SEEN,
        application_state=ApplicationState.DISCOVERED,
        mode=ApplicationMode.ASSIST,
        correlation_id=correlation_id,
    )


def _analyze_and_maybe_generate(job_id: int, stats: dict) -> None:
    analyzed = analyze_job(job_id)
    stats["jobs_analyzed"] += 1

    if analyzed.application_state.value.startswith("SKIPPED"):
        stats["hard_skips"] += 1
        return

    if analyzed.application_state != ApplicationState.ANALYZED:
        return

    if analyzed.sponsorship_status == SponsorshipStatus.UNKNOWN:
        return  # do not apply, per policy

    if analyzed.sponsorship_status == SponsorshipStatus.CONFIRMED_SPONSOR:
        stats["confirmed_sponsors"] += 1
    elif analyzed.sponsorship_status == SponsorshipStatus.LIKELY_SPONSOR:
        stats["likely_sponsors"] += 1

    result = generate_assist_outputs(job_id)
    if result.application_state in (ApplicationState.READY_TO_APPLY, ApplicationState.REVIEW_REQUIRED):
        stats["packages_generated"] += 1


def _process_raw_job(
    raw: RawJobPosting, stats: dict, cycle_id: int | None = None, registry_id: int | None = None,
    correlation_id: str = "",
) -> str:
    """Fetch -> filter -> dedupe -> store -> analyze one raw posting. Returns
    'filtered' | 'duplicate' | 'new' for the caller's per-tenant observability
    counters. Cross-provider dedup checks, in order: stable provider ID,
    canonical URL, then the company/title/location fingerprint fallback."""
    if _pre_filter_reason(raw) is not None:
        return "filtered"

    fp = fingerprint(raw.company, raw.title, raw.location)
    canonical = canonicalize_url(raw.source_url or raw.url)
    existing = get_job_by_provider_external_id(raw.provider, raw.external_job_id)
    if existing is None and canonical:
        existing = get_job_by_canonical_url(canonical)
    if existing is None and not canonical:
        # Weak fallback -- only trusted when there is no URL at all to
        # disambiguate by (e.g. a manually-pasted JD). When a canonical URL
        # IS available but didn't match anything, that is strong evidence
        # this is a genuinely different requisition, even if title/company/
        # location happen to coincide with another job (see docs/company-registry.md
        # and CLAUDE.md dedup rules -- never merge on a weak signal alone).
        existing = get_job_by_fingerprint(fp)

    now_iso = datetime.now(timezone.utc).isoformat()

    if existing is not None:
        stats["jobs_deduplicated"] += 1
        touch_last_seen(existing.id, now_iso)
        record_provenance(
            existing.id, raw.provider, raw.external_job_id,
            source_url=raw.source_url or raw.url, registry_id=registry_id, discovery_cycle_id=cycle_id,
        )
        # Never regenerate/duplicate an application package on a later cycle --
        # only (re)analyze a job that was discovered but never analyzed yet.
        if existing.application_state in (ApplicationState.DISCOVERED, ApplicationState.NEW):
            _analyze_and_maybe_generate(existing.id, stats)
        return "duplicate"

    job = _raw_to_job(raw, canonical, correlation_id=correlation_id)
    job_id = insert_job(job)
    stats["jobs_new"] += 1
    record_provenance(
        job_id, raw.provider, raw.external_job_id,
        source_url=raw.source_url or raw.url, registry_id=registry_id, discovery_cycle_id=cycle_id,
    )
    _analyze_and_maybe_generate(job_id, stats)
    return "new"


# Public aliases for reuse by the Phase 5 worker fleet (app/workers/runner.py)
# -- same fetch->filter->dedupe->store->analyze pipeline, no duplicated logic.
process_raw_job = _process_raw_job
analyze_and_maybe_generate = _analyze_and_maybe_generate


def _discover_from_static_config(cycle_id: int, stats: dict) -> list[str]:
    """Legacy Phase 2 path: providers configured via ENABLED_PROVIDERS /
    *_BOARD_TOKENS env vars. Unchanged behavior, preserved for backward
    compatibility with existing deployments and tests."""
    providers = get_enabled_providers()
    provider_names = [p.name for p in providers]

    for provider in providers:
        try:
            raw_jobs = provider.fetch_jobs(max_jobs=config.MAX_JOBS_PER_CYCLE)
        except Exception as exc:
            stats["errors"].append(f"{provider.name}: fetch failed: {exc}")
            logger.warning("provider %s fetch failed", provider.name, exc_info=True)
            continue

        for raw in raw_jobs:
            if stats["jobs_fetched"] >= config.MAX_JOBS_PER_CYCLE:
                break
            stats["jobs_fetched"] += 1
            try:
                _process_raw_job(raw, stats, cycle_id=cycle_id)
            except Exception as exc:
                stats["errors"].append(f"{provider.name}/{raw.external_job_id}: {exc}")
                logger.exception("failed processing job %s/%s", provider.name, raw.external_job_id)
                continue

    return provider_names


def _discover_from_registry(cycle_id: int, stats: dict) -> list[str]:
    """Phase 3 path: per-tenant discovery driven by the company_registry
    table, with adaptive polling and per-tenant health/observability. A
    failing tenant is marked degraded/failing and skipped on future cycles
    until it backs off far enough; other tenants keep processing normally.

    Daily-use-v1: bounded by its OWN `DISCOVERY_REGISTRY_MAX_JOBS_PER_CYCLE`
    budget, never the legacy static-provider phase's `MAX_JOBS_PER_CYCLE` --
    the two phases previously shared one counter, so a legacy provider
    fetching its own full `MAX_JOBS_PER_CYCLE` FIRST in the same cycle could
    silently leave zero budget for every company_registry tenant, no matter
    how many were genuinely due (a real, reproduced starvation bug: with the
    default config, this phase never processed a single due tenant)."""
    due = list_due_for_poll(limit=200)
    tenant_provider_names: list[str] = []
    registry_jobs_fetched = 0

    for entry in due:
        if registry_jobs_fetched >= config.DISCOVERY_REGISTRY_MAX_JOBS_PER_CYCLE:
            break

        started = datetime.now(timezone.utc)
        provider = build_provider_for_tenant(entry.provider, entry.tenant_identifier)
        if provider is None:
            continue
        tenant_provider_names.append(entry.provider)

        try:
            raw_jobs = provider.fetch_jobs(max_jobs=config.MAX_JOBS_PER_PROVIDER)
        except Exception as exc:
            finished = datetime.now(timezone.utc)
            latency_ms = (finished - started).total_seconds() * 1000
            error_type = type(exc).__name__
            stats["errors"].append(f"{entry.provider}/{entry.tenant_identifier}: fetch failed: {exc}")
            logger.warning("registry tenant %s/%s fetch failed", entry.provider, entry.tenant_identifier, exc_info=True)
            mark_poll_result(entry.id, success=False, jobs_new=0, latency_ms=latency_ms, error=str(exc))
            insert_discovery_log({
                "cycle_id": cycle_id, "provider": entry.provider, "company": entry.company_name,
                "tenant": entry.tenant_identifier, "started_at": started.isoformat(),
                "finished_at": finished.isoformat(), "latency_ms": latency_ms,
                "jobs_received": 0, "jobs_new": 0, "jobs_duplicate": 0, "jobs_filtered": 0,
                "error_type": error_type,
            })
            continue

        jobs_received = len(raw_jobs)
        new_count = duplicate_count = filtered_count = 0

        for raw in raw_jobs:
            if registry_jobs_fetched >= config.DISCOVERY_REGISTRY_MAX_JOBS_PER_CYCLE:
                break
            registry_jobs_fetched += 1
            stats["jobs_fetched"] += 1
            try:
                status = _process_raw_job(raw, stats, cycle_id=cycle_id, registry_id=entry.id)
            except Exception as exc:
                stats["errors"].append(f"{entry.provider}/{raw.external_job_id}: {exc}")
                logger.exception("failed processing job %s/%s", entry.provider, raw.external_job_id)
                continue
            if status == "new":
                new_count += 1
            elif status == "duplicate":
                duplicate_count += 1
            elif status == "filtered":
                filtered_count += 1

        finished = datetime.now(timezone.utc)
        latency_ms = (finished - started).total_seconds() * 1000
        mark_poll_result(entry.id, success=True, jobs_new=new_count, latency_ms=latency_ms)
        insert_discovery_log({
            "cycle_id": cycle_id, "provider": entry.provider, "company": entry.company_name,
            "tenant": entry.tenant_identifier, "started_at": started.isoformat(),
            "finished_at": finished.isoformat(), "latency_ms": latency_ms,
            "jobs_received": jobs_received, "jobs_new": new_count, "jobs_duplicate": duplicate_count,
            "jobs_filtered": filtered_count, "error_type": "",
        })

    return tenant_provider_names


def run_discovery_cycle() -> dict:
    """The autonomous discovery cycle: fetch -> normalize -> dedupe -> classify
    -> score -> gate -> generate -> persist -> log. Isolates provider and
    per-job errors so one failure never aborts the rest of the cycle. Runs
    two phases: the legacy statically-configured providers (Phase 2,
    unchanged), then any company_registry tenants due for adaptive polling
    (Phase 3)."""
    started = datetime.now(timezone.utc)
    stats = dict(
        jobs_fetched=0, jobs_new=0, jobs_deduplicated=0, jobs_analyzed=0,
        confirmed_sponsors=0, likely_sponsors=0, hard_skips=0,
        packages_generated=0, errors=[],
    )

    cycle_id = start_discovery_cycle(started.isoformat(), [])

    static_names = _discover_from_static_config(cycle_id, stats)
    registry_names = _discover_from_registry(cycle_id, stats)
    provider_names = sorted(set(static_names) | set(registry_names))

    finished = datetime.now(timezone.utc)
    summary = {
        **stats,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_seconds": (finished - started).total_seconds(),
        "providers": provider_names,
    }
    finalize_discovery_cycle(cycle_id, summary)
    logger.info(
        "discovery cycle complete: fetched=%s new=%s analyzed=%s packages=%s errors=%s",
        stats["jobs_fetched"], stats["jobs_new"], stats["jobs_analyzed"],
        stats["packages_generated"], len(stats["errors"]),
    )
    return summary
