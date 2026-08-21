import logging
from datetime import datetime, timezone

from app import config
from app.discovery.dedup import fingerprint
from app.freshness.tracker import compute_age_minutes
from app.jobs_repo import (
    get_job_by_fingerprint,
    get_job_by_provider_external_id,
    insert_discovery_cycle,
    insert_job,
    touch_last_seen,
)
from app.matching.employment_type import is_full_time
from app.matching.geography import is_us_location
from app.models import ApplicationMode, ApplicationState, Job, SponsorshipStatus
from app.pipeline import analyze_job, generate_assist_outputs
from app.providers.base import RawJobPosting
from app.providers.registry import get_enabled_providers

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


def _raw_to_job(raw: RawJobPosting) -> Job:
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
        published_at=raw.published_at,
        application_state=ApplicationState.DISCOVERED,
        mode=ApplicationMode.ASSIST,
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


def _process_raw_job(raw: RawJobPosting, stats: dict) -> None:
    if _pre_filter_reason(raw) is not None:
        return  # discarded before storage -- not counted as new or duplicate

    fp = fingerprint(raw.company, raw.title, raw.location)
    existing = get_job_by_provider_external_id(raw.provider, raw.external_job_id)
    if existing is None:
        existing = get_job_by_fingerprint(fp)

    now_iso = datetime.now(timezone.utc).isoformat()

    if existing is not None:
        stats["jobs_deduplicated"] += 1
        touch_last_seen(existing.id, now_iso)
        # Never regenerate/duplicate an application package on a later cycle --
        # only (re)analyze a job that was discovered but never analyzed yet.
        if existing.application_state in (ApplicationState.DISCOVERED, ApplicationState.NEW):
            _analyze_and_maybe_generate(existing.id, stats)
        return

    job = _raw_to_job(raw)
    job_id = insert_job(job)
    stats["jobs_new"] += 1
    _analyze_and_maybe_generate(job_id, stats)


def run_discovery_cycle() -> dict:
    """The autonomous discovery cycle: fetch -> normalize -> dedupe -> classify
    -> score -> gate -> generate -> persist -> log. Isolates provider and
    per-job errors so one failure never aborts the rest of the cycle."""
    started = datetime.now(timezone.utc)
    stats = dict(
        jobs_fetched=0, jobs_new=0, jobs_deduplicated=0, jobs_analyzed=0,
        confirmed_sponsors=0, likely_sponsors=0, hard_skips=0,
        packages_generated=0, errors=[],
    )
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
                _process_raw_job(raw, stats)
            except Exception as exc:
                stats["errors"].append(f"{provider.name}/{raw.external_job_id}: {exc}")
                logger.exception("failed processing job %s/%s", provider.name, raw.external_job_id)
                continue

    finished = datetime.now(timezone.utc)
    summary = {
        **stats,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_seconds": (finished - started).total_seconds(),
        "providers": provider_names,
    }
    insert_discovery_cycle(summary)
    logger.info(
        "discovery cycle complete: fetched=%s new=%s analyzed=%s packages=%s errors=%s",
        stats["jobs_fetched"], stats["jobs_new"], stats["jobs_analyzed"],
        stats["packages_generated"], len(stats["errors"]),
    )
    return summary
