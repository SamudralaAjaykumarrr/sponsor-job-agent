import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"
CANDIDATE_DIR = BASE_DIR / "candidate_data"

DB_PATH = DATA_DIR / "app.db"
KNOWN_SPONSORS_PATH = DATA_DIR / "known_h1b_sponsors.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
CANDIDATE_DIR.mkdir(parents=True, exist_ok=True)

NEEDS_USER_INPUT = "NEEDS_USER_INPUT"

DEFAULT_MODE = "ASSIST"
VALID_MODES = ("ANALYZE", "ASSIST", "AUTO")


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader -- avoids adding a python-dotenv dependency for a
    handful of KEY=VALUE lines. Never overrides already-set environment vars."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(BASE_DIR / ".env")


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or not v.strip():
        return default
    try:
        return int(v)
    except ValueError:
        return default


def _env_list(name: str, default: list[str]) -> list[str]:
    v = os.getenv(name)
    if v is None:
        return default
    return [x.strip() for x in v.split(",") if x.strip()]


def _env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or not v.strip():
        return default
    try:
        return float(v)
    except ValueError:
        return default


# --- Autonomous agent configuration -----------------------------------------
# Safe defaults: agent OFF until the user opts in (per "Default mode should be
# ASSIST, not blind auto-apply" and "Keep safe defaults").
AGENT_ENABLED = _env_bool("AGENT_ENABLED", False)
DISCOVERY_INTERVAL_MINUTES = _env_int("DISCOVERY_INTERVAL_MINUTES", 15)
MAX_JOBS_PER_CYCLE = _env_int("MAX_JOBS_PER_CYCLE", 25)
MIN_MATCH_SCORE = _env_int("MIN_MATCH_SCORE", 25)
FRESHNESS_MAX_DAYS = _env_int("FRESHNESS_MAX_DAYS", 3)
MIN_SALARY_USD = _env_int("MIN_SALARY_USD", 80000)

ENABLED_PROVIDERS = _env_list("ENABLED_PROVIDERS", ["greenhouse", "lever"])
# Illustrative example boards -- verify these are still valid/public before
# relying on them; add/remove company slugs in .env as desired.
GREENHOUSE_BOARD_TOKENS = _env_list("GREENHOUSE_BOARD_TOKENS", ["gitlab"])
LEVER_COMPANY_SLUGS = _env_list("LEVER_COMPANY_SLUGS", ["leverdemo"])

# Phase 3 additional connectors -- comma-separated tenant identifiers per
# provider. Empty by default; the provider only does anything if both
# enabled (ENABLED_PROVIDERS) AND given at least one tenant identifier here.
ASHBY_JOB_BOARD_NAMES = _env_list("ASHBY_JOB_BOARD_NAMES", [])
WORKABLE_ACCOUNT_SUBDOMAINS = _env_list("WORKABLE_ACCOUNT_SUBDOMAINS", [])
SMARTRECRUITERS_COMPANY_IDS = _env_list("SMARTRECRUITERS_COMPANY_IDS", [])
BAMBOOHR_SUBDOMAINS = _env_list("BAMBOOHR_SUBDOMAINS", [])
RECRUITEE_SUBDOMAINS = _env_list("RECRUITEE_SUBDOMAINS", [])
BREEZY_SUBDOMAINS = _env_list("BREEZY_SUBDOMAINS", [])
TEAMTAILOR_SUBDOMAINS = _env_list("TEAMTAILOR_SUBDOMAINS", [])
# Comeet requires the public embed token shown on the company's own careers
# page (not a secret) -- configure as "company:token" pairs.
COMEET_COMPANY_TOKENS = _env_list("COMEET_COMPANY_TOKENS", [])
# Workday tenants vary too much to guess a URL pattern -- each entry is a full
# base URL of the form "https://{tenant}.{wdHost}/wday/cxs/{tenant}/{site}".
WORKDAY_TENANT_BASE_URLS = _env_list("WORKDAY_TENANT_BASE_URLS", [])

# --- Provider HTTP / pagination / scheduling hardening ----------------------
PROVIDER_HTTP_TIMEOUT_SECONDS = _env_float("PROVIDER_HTTP_TIMEOUT_SECONDS", 10.0)
PROVIDER_MAX_RETRIES = _env_int("PROVIDER_MAX_RETRIES", 2)
PROVIDER_MAX_RESPONSE_BYTES = _env_int("PROVIDER_MAX_RESPONSE_BYTES", 5_000_000)
PROVIDER_CONCURRENCY_LIMIT = _env_int("PROVIDER_CONCURRENCY_LIMIT", 5)
PROVIDER_USER_AGENT = os.getenv(
    "PROVIDER_USER_AGENT",
    "SponsorJobAgent/1.0 (+local job-discovery agent; contact=candidate; respects robots/ToS)",
)

MAX_PAGES_PER_PROVIDER = _env_int("MAX_PAGES_PER_PROVIDER", 20)
MAX_JOBS_PER_PROVIDER = _env_int("MAX_JOBS_PER_PROVIDER", 500)

PROVIDER_DEFAULT_POLL_MINUTES = _env_int("PROVIDER_DEFAULT_POLL_MINUTES", 15)
PROVIDER_MIN_POLL_MINUTES = _env_int("PROVIDER_MIN_POLL_MINUTES", 10)
PROVIDER_MAX_POLL_MINUTES = _env_int("PROVIDER_MAX_POLL_MINUTES", 240)

# Whether to auto-populate a handful of illustrative demo entries into the
# company_registry table on first init. Off by default -- the registry is
# meant to be populated deliberately (Phase 4 importer), not with fabricated
# example companies in a real deployment.
REGISTRY_SEED_DEMO_DATA = _env_bool("REGISTRY_SEED_DEMO_DATA", False)

# --- Phase 4: registry acquisition/verification/lifecycle scale settings ---
# Deterministic partitioning for a future distributed worker (Phase 7). Local
# default is 1 shard / index 0 -- no behavior change unless configured.
REGISTRY_SHARD_COUNT = _env_int("REGISTRY_SHARD_COUNT", 1)
REGISTRY_SHARD_INDEX = _env_int("REGISTRY_SHARD_INDEX", 0)

# Backpressure: how many due portals to pull into memory per batch, and the
# overall wall-clock budget for one registry verification/poll cycle. Neither
# is "poll 100k portals" -- these bound one local run regardless of registry size.
REGISTRY_DUE_BATCH_SIZE = _env_int("REGISTRY_DUE_BATCH_SIZE", 50)
DISCOVERY_CYCLE_TIME_BUDGET_SECONDS = _env_int("DISCOVERY_CYCLE_TIME_BUDGET_SECONDS", 60)
REGISTRY_MAX_PORTALS_PER_CYCLE = _env_int("REGISTRY_MAX_PORTALS_PER_CYCLE", 50)

# Verification pipeline: how many jobs to request when probing a candidate
# portal's endpoint (kept tiny -- this is a structural check, not a poll).
REGISTRY_VERIFICATION_PROBE_JOBS = _env_int("REGISTRY_VERIFICATION_PROBE_JOBS", 5)
# Consecutive PERMANENT (e.g. repeated 404) failures before an ACTIVE/VERIFIED
# portal is demoted to STALE -- distinct from transient-failure backoff.
REGISTRY_STALE_AFTER_PERMANENT_FAILURES = _env_int("REGISTRY_STALE_AFTER_PERMANENT_FAILURES", 5)

# --- Phase 4: safe bounded career-page discovery ---------------------------
PAGE_DISCOVERY_MAX_PAGES = _env_int("PAGE_DISCOVERY_MAX_PAGES", 9)
PAGE_DISCOVERY_TIMEOUT_SECONDS = _env_float("PAGE_DISCOVERY_TIMEOUT_SECONDS", 8.0)
PAGE_DISCOVERY_MAX_RESPONSE_BYTES = _env_int("PAGE_DISCOVERY_MAX_RESPONSE_BYTES", 2_000_000)
PAGE_DISCOVERY_MAX_REDIRECTS = _env_int("PAGE_DISCOVERY_MAX_REDIRECTS", 3)
PAGE_DISCOVERY_CONCURRENCY_LIMIT = _env_int("PAGE_DISCOVERY_CONCURRENCY_LIMIT", 3)
PAGE_DISCOVERY_RESPECT_ROBOTS = _env_bool("PAGE_DISCOVERY_RESPECT_ROBOTS", True)

# --- Phase 5: distributed polling execution layer ---------------------------
# See docs/phase5-distributed-polling.md, docs/worker-architecture.md,
# docs/polling-leases.md. All local-mode by default -- SQLite-backed,
# same DB file, no external services required.

# How many portals one worker processes concurrently (bounded thread pool).
POLL_WORKER_CONCURRENCY = _env_int("POLL_WORKER_CONCURRENCY", 4)
# How long a worker owns a leased portal before the lease is reclaimable by
# another worker if this one crashes/hangs.
PORTAL_LEASE_SECONDS = _env_int("PORTAL_LEASE_SECONDS", 120)
# How often a running worker updates its heartbeat row.
WORKER_HEARTBEAT_SECONDS = _env_int("WORKER_HEARTBEAT_SECONDS", 15)

# Note: REGISTRY_SHARD_COUNT / REGISTRY_SHARD_INDEX were already defined
# above (Phase 4 groundwork); Phase 5's worker runner is what actually
# enforces them now (app/workers/leasing.py), see docs/polling-leases.md.

# How many due work items one claim_due_work() call pulls into memory at
# once (per queue, per worker cycle) -- bounded regardless of registry size.
DUE_WORK_BATCH_SIZE = _env_int("DUE_WORK_BATCH_SIZE", 50)
# Wall-clock budget for one worker poll cycle (claim -> process -> repeat)
# before the worker stops claiming new work and loops back to housekeeping.
POLL_CYCLE_TIME_BUDGET_SECONDS = _env_int("POLL_CYCLE_TIME_BUDGET_SECONDS", 60)
# Hard cap on portals processed by one worker in one cycle, independent of
# the time budget.
MAX_PORTALS_PER_WORKER_CYCLE = _env_int("MAX_PORTALS_PER_WORKER_CYCLE", 50)

# Max concurrent in-flight requests to one provider, enforced across ALL
# worker processes via a DB-backed slot counter (app/workers/circuit.py) --
# one overloaded provider never starves requests to a different provider.
PROVIDER_CONCURRENCY_DEFAULT = _env_int("PROVIDER_CONCURRENCY_DEFAULT", 3)

# Dead-letter: after this many CONSECUTIVE PERMANENT failures (not transient
# ones) a work item is disabled and recorded in the dead-letter table
# instead of being retried forever. Separate from
# REGISTRY_STALE_AFTER_PERMANENT_FAILURES (Phase 4's verification-lifecycle
# threshold, which demotes rather than dead-letters).
DEAD_LETTER_MAX_ATTEMPTS = _env_int("DEAD_LETTER_MAX_ATTEMPTS", 8)

# Circuit breaker: fraction (0.0-1.0) of recent attempts to a provider that
# must fail, once a minimum sample size has been observed, before that
# provider's polling is paused fleet-wide.
CIRCUIT_BREAKER_FAILURE_THRESHOLD = _env_float("CIRCUIT_BREAKER_FAILURE_THRESHOLD", 0.5)
# How long a tripped (OPEN) provider circuit stays paused before allowing a
# single low-frequency HALF_OPEN probe attempt through.
CIRCUIT_BREAKER_COOLDOWN_SECONDS = _env_int("CIRCUIT_BREAKER_COOLDOWN_SECONDS", 120)

# Graceful shutdown: bounded grace period for in-flight portal attempts to
# finish after SIGINT/SIGTERM before the worker exits regardless.
WORKER_SHUTDOWN_GRACE_SECONDS = _env_int("WORKER_SHUTDOWN_GRACE_SECONDS", 20)

# Local process supervisor (app/workers/supervisor.py): bounded worker count.
SUPERVISOR_MAX_WORKERS = _env_int("SUPERVISOR_MAX_WORKERS", 8)

# --- Phase 6: production-scale distributed architecture --------------------
# See docs/phase6-production-scale.md, docs/postgres-backend.md,
# docs/distributed-workers.md, docs/provider-error-contract.md.

# DATABASE_URL itself is read directly by app/db.py (not mirrored here) so
# it can be monkeypatched the same way DB_PATH already is in tests.

# Orphan worker reaper (CLAUDE.md Phase 6 section 20): a worker is marked
# OFFLINE only after its heartbeat is this many seconds stale -- deliberately
# several heartbeat intervals (not one) so ordinary jitter (a slow cycle, a
# GC pause) is never mistaken for a crash. Actual lease recovery is
# independent of this value (see app/workers/leasing.py's own
# lease_expires_at) -- this setting only affects OFFLINE dashboard/CLI
# visibility.
ORPHAN_WORKER_STALE_SECONDS = _env_int("ORPHAN_WORKER_STALE_SECONDS", WORKER_HEARTBEAT_SECONDS * 6)

# Postgres connection pooling / statement timeout hardening. Kept modest and
# explicit rather than unbounded -- a hung query must never wedge a worker
# or the dashboard forever.
POSTGRES_STATEMENT_TIMEOUT_MS = _env_int("POSTGRES_STATEMENT_TIMEOUT_MS", 30_000)

# Distributed rate limiting (CLAUDE.md Phase 6 section 18) reuses the same
# DB-backed `provider_circuit_state.inflight` slot counter Phase 5 already
# built (app/workers/circuit.py::acquire_inflight_slot/release_inflight_slot)
# -- it is already fleet-wide the moment DATABASE_URL points at a shared
# Postgres instance, since every worker process (on any machine) reads/
# writes the same row. PROVIDER_CONCURRENCY_DEFAULT above is the shared
# budget; no second, different mechanism is introduced.

# Prometheus-format /metrics endpoint (CLAUDE.md Phase 6 section 30).
METRICS_ENABLED = _env_bool("METRICS_ENABLED", True)

# Structured JSON logging (CLAUDE.md Phase 6 section 35). Off by default for
# local development (plain text logs are easier to read in a terminal);
# turn on for production deployments where a log aggregator expects JSON.
STRUCTURED_LOGGING_ENABLED = _env_bool("STRUCTURED_LOGGING_ENABLED", False)

# Schema drift (CLAUDE.md Phase 6 section 16/17): if this many DISTINCT
# tenants of the same provider show schema drift within SCHEMA_DRIFT_WINDOW_HOURS,
# it's treated as provider-wide (not one oddball tenant) and fed into the
# existing circuit breaker as a failure signal -- never a second, separate
# breaker mechanism.
SCHEMA_DRIFT_CIRCUIT_TENANT_THRESHOLD = _env_int("SCHEMA_DRIFT_CIRCUIT_TENANT_THRESHOLD", 3)
SCHEMA_DRIFT_WINDOW_HOURS = _env_float("SCHEMA_DRIFT_WINDOW_HOURS", 1.0)
