# Scaling Claims Policy

**Durable rule** (also recorded in `CLAUDE.md`): never say *"monitoring N
portals"* unless operational metrics demonstrate those N portals were
actually polled within their expected schedule. Storing a row is not the
same as monitoring a company.

## The allowed vocabulary

| Say this | Not this | Why |
|---|---|---|
| "registry contains N portals" | "monitoring N portals" | A stored row proves nothing about whether it's ever been polled |
| "N verified" | "N confirmed live" (without saying *when*) | Verification is a point-in-time check; staleness matters |
| "N active" | "N being monitored" | Active = eligible to be polled, not proof it was |
| "N successfully polled in the last 24h" | "N monitored" | This is the only phrase backed by real, timestamped attempt history |
| "monitoring coverage: X%" | "X% uptime" | Coverage is *scheduled portals actually polled on schedule*, not an SLA measurement |

## Where this is enforced in code, not just prose

`app/workers/metrics.py::fleet_snapshot()` is the single source of truth for
every number a dashboard or report can honestly cite. It distinguishes:

```
stored_companies / stored_portals          -- registry_companies / registry_portals row counts
candidate_portals / verified_portals /
    active_portals                         -- registry_portals verification_status breakdown
operational_poll_targets                   -- ENABLED company_registry rows (eligible to be polled)
actually_polled_last_1h / last_24h         -- DISTINCT portal ids with a SUCCEEDED poll_attempts row
                                               in that window -- real, timestamped, queryable evidence
monitoring_coverage_24h                    -- |actually_polled_last_24h ∩ operational_poll_targets|
                                               / |operational_poll_targets|
```

`monitoring_coverage_24h` is explicitly an intersection with the *currently*
enabled target set, not a raw count — a portal that was dead-lettered or
disabled since its last successful poll doesn't inflate the denominator.

`discovery_latency_percentiles()` computes `first_seen_at - published_at`
**only** for jobs whose `freshness_source == 'PUBLISHED_AT'** — i.e. the
provider actually supplied a real, structured timestamp
(`app.providers.base.RawJobPosting.published_at`). A job whose freshness
falls back to `FIRST_SEEN` (the provider gave nothing, or only a relative
string like "posted 3 days ago", which Phase 3's connectors already refuse
to fabricate into a fake timestamp) is silently excluded from the latency
sample, never treated as a zero-latency data point. See
`test_discovery_latency_ignores_fabricated_timestamps`.

## What this phase actually demonstrated, numbers included

From the real, live 2-worker validation run against the actual verified
registry (not a simulation) during this build (see the final completion
report for the full breakdown):

- **Stored**: 26 companies, 26 portals (after the real acquisition growth
  test added 6 more candidates, 3 of which verified).
- **Active** (eligible to be polled): 22.
- **Actually polled in the session**: 17 of 22, converging toward all 22
  across successive `--once` cycles (a single bounded cycle under a tight
  shared-provider concurrency budget doesn't always finish everyone in one
  pass — continuous operation does).
- **Never claimed**: "monitoring 22 portals" as a standing fact — only
  "22 active, 17 successfully polled in this session's most recent window,"
  which is what the numbers actually proved.

## What would make a larger number honest

Storing 50,000 rows via bulk import proves nothing about monitoring 50,000
portals. What *would*:

1. All 50,000 (or however many) are `ACTIVE` — meaning each individually
   passed live verification at some point (see `docs/registry-acquisition.md`).
2. A fleet of workers, running continuously (not `--once`), demonstrably
   keeps `actually_polled_last_24h` near `operational_poll_targets` — i.e.
   `monitoring_coverage_24h` stays close to 1.0 over time, not just in one
   snapshot.
3. `dead_letters_open` and `provider_circuits_open_or_half_open` stay low
   relative to fleet size — evidence the fleet is keeping up, not silently
   falling behind while `operational_poll_targets` keeps climbing.

None of that is claimed here for 50,000 — see the completion report for
exactly what blocks it today (this build only grew the real registry to 26
companies / 22 active portals; scaling further requires either a larger
legitimate, attributable seed dataset or more acquisition batches like
`phase5_growth_seed.csv`, and, at real scale, moving past SQLite — see
below).

## SQLite's honest ceiling

**Local/development**: SQLite (WAL mode, `busy_timeout`) — correct,
serialized, multi-*process* concurrency on one machine or one shared
filesystem. This is what Phase 5 ships and what every number above was
measured against.

**Future distributed production**: PostgreSQL (or another real
client-server transactional database) + optionally an external queue
(SQS/Redis) — required once "multiple machines" becomes real rather than
simulated via `--shard-count`/`--shard-index` on one host. `docs/worker-
architecture.md`'s "Queue abstraction" section documents exactly what that
swap involves; no provider/pipeline code depends on SQLite's specific
locking behavior today, only `app/workers/leasing.py` and `app/db.py` do.

**SQLite is never claimed as "the final database for 100k portals across
many machines."** The synthetic benchmark in `scripts/worker_benchmark.py`
proves the *query/leasing layer* holds up at 100k rows on one machine (zero
duplicate claims across 8 concurrent threads, sub-25ms bounded due-queries)
— it says nothing about network capacity, and nothing about multi-machine
operation, which SQLite does not support.
