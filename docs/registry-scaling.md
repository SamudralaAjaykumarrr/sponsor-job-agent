# Registry Scaling

## What "supports 100k+ portals" means here — and what it doesn't

**Supporting 100,000 stored portal records is not the same as successfully polling 100,000
portals from a single laptop.** This phase built and benchmarked the former. Distributed
execution (actually fetching from tens of thousands of live endpoints on a schedule) is future
work — see "What remains" below and CLAUDE.md section 38 ("do NOT implement... distributed cloud
deployment").

## Synthetic scale benchmark (`scripts/registry_benchmark.py`)

Generates synthetic rows **only** in a throwaway temp SQLite file (`tempfile.mkdtemp()`), never
the real `data/app.db` — `app.config.DB_PATH`/`app.db.DB_PATH` are repointed before any row is
written, and every synthetic row uses `provider="benchmark-fixture"` (never a real provider name)
and `tenant_identifier="synthetic-N"`, so it could never be mistaken for a real entry.

```
python3 scripts/registry_benchmark.py --sizes 1000,10000,50000 [--include-100k]
```

### Results (this machine, this session — see the final Phase 4 report for the exact numbers)

| Rows | Bulk import | Dedup lookup (500x) | Due-portal query (LIMIT 200) | Pagination (2 pages) | Shard assignment (all rows) | JSONL export |
|---|---|---|---|---|---|---|
| 1,000 | ~0.02s | ~0.08s | ~0.003s | ~0.006s | ~0.001s | ~0.02s |
| 10,000 | ~0.12s | ~0.03s | ~0.002s | ~0.004s | ~0.009s | ~0.21s |
| 50,000 | ~0.54s | ~0.02s | ~0.002s | ~0.004s | ~0.05s | ~0.95s |
| 100,000 | ~1.09s | ~0.03s | ~0.002s | ~0.005s | ~0.10s | ~1.98s |

The bounded queries (due-portal, pagination) stay flat regardless of table size — that's the
point of `LIMIT` + keyset pagination over `SELECT *`. Bulk import and export scale roughly
linearly, as expected for row-count-proportional work; both stay well under a second even at
50k, and under 2s at 100k, on ordinary SQLite with no special tuning beyond the indexes already
in `app/db.py`.

This says nothing about network-polling throughput — it is purely a storage/query benchmark.

## How this stays fast structurally

- Every list/query in `app/registry/store.py` takes a `limit` and uses keyset pagination
  (`id > after_id`), never offset pagination or `SELECT *`.
- Unique indexes (`app/db.py`): `(provider, tenant_identifier)` and `canonical_url` on
  `registry_portals`, `(normalized_name, primary_domain)` on `registry_companies`,
  `(portal_id, source_type, source_name)` on `registry_provenance` — dedup lookups are O(log n)
  index seeks, not table scans.
- Bulk import batches writes (`--batch-size`, default 500) so a huge input file streams through
  without buffering the whole thing, and each row's DB work is one small transaction.
- Export (`app/registry/export.py`) streams in 1,000-row keyset-paginated batches, writing as it
  goes — never materializes the whole registry in memory.

## Partitioning (deterministic sharding)

`app/registry/sharding.py::shard_for_portal(portal_id, shard_count)` — SHA-256 hash mod
shard_count. Deterministic, no synchronized-instant thundering herd, every id maps to exactly one
shard (tested up to several thousand ids with roughly-even distribution). `REGISTRY_SHARD_COUNT`/
`REGISTRY_SHARD_INDEX` default to 1/0 today; this is groundwork only — no worker process reads
these yet.

## What remains before genuinely monitoring 50,000+ live portals

1. **A distributed poller.** Today, one process (`app/agent/cycle.py`) polls whatever's due,
   bounded by `MAX_JOBS_PER_CYCLE`/`REGISTRY_DUE_BATCH_SIZE`. At 50k+ *active* portals this single
   process would need to either run near-continuously or be split across `REGISTRY_SHARD_COUNT`
   workers — the shard function exists, the worker/dispatch layer does not.
2. **Real acquisition at scale.** This phase's real seed is ~20 companies, deliberately small per
   CLAUDE.md's no-fake-scale rule. Reaching 50k+ *verified* portals requires either many more bulk
   imports from legitimate external sources, or a lot more page-discovery runs — both are now
   possible with the tooling built here, but doing it is future, ongoing operational work, not a
   code change.
3. **Provider-specific rate-limit policy tuning** at real fleet scale (current handling is
   generic bounded-retry-with-backoff, `app/providers/http_client.py`; per-provider policy
   objects would help once dozens of tenants per provider are actually being polled concurrently).
4. **Persistent job queues / durable scheduling** if moving beyond a single in-process asyncio
   loop — today's `AgentScheduler` is adequate for the current single-process, single-shard
   deployment shape.
