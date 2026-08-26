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

# --- Phase 8: safe ATS application executor ---------------------------------
# See docs/phase8-application-executor.md, docs/application-safety.md.
#
# Safe defaults (CLAUDE.md Phase 8 sections 63-64): BOTH off until the user
# explicitly opts in. Discovery/analysis/resume generation continue exactly
# as before regardless of these two flags -- only queuing/executing/
# submitting applications is gated by them.
APPLICATION_EXECUTOR_ENABLED = _env_bool("APPLICATION_EXECUTOR_ENABLED", False)
AUTO_SUBMIT_ENABLED = _env_bool("AUTO_SUBMIT_ENABLED", False)

# Minimum technical match score (0-100) required to enter the application
# executor queue at all -- a distinct, and may be set stricter than,
# MIN_MATCH_SCORE above (which only gates resume generation).
MIN_APPLICATION_MATCH_SCORE = _env_int("MIN_APPLICATION_MATCH_SCORE", MIN_MATCH_SCORE)

# Rate limiting (CLAUDE.md Phase 8 sections 46, 62): enforced by querying
# application_executions timestamps directly (no separate counter table),
# so this is already fleet-wide the moment DATABASE_URL points at a shared
# Postgres instance -- same principle as Phase 6's distributed rate limiting.
MAX_APPLICATIONS_PER_HOUR = _env_int("MAX_APPLICATIONS_PER_HOUR", 5)
MAX_APPLICATIONS_PER_DAY = _env_int("MAX_APPLICATIONS_PER_DAY", 20)
MAX_APPLICATIONS_PER_COMPANY_PER_DAY = _env_int("MAX_APPLICATIONS_PER_COMPANY_PER_DAY", 2)
# Apply/Automation Settings V1: optional, consumer-configurable limits.
# 0 means "no limit" for both -- app.applications.rate_limit only enforces
# them when non-zero, so the safe default changes nothing about existing
# behavior. Persisted/live-editable via app/settings_store.py's existing
# ALLOWED_SETTINGS mechanism (see settings_store.py), same as the two above.
MAX_APPLICATIONS_PER_WEEK = _env_int("MAX_APPLICATIONS_PER_WEEK", 0)
MAX_CONCURRENT_APPLICATIONS = _env_int("MAX_CONCURRENT_APPLICATIONS", 0)

# How long an application-execution lease is held before another
# executor-capable worker may reclaim it (same lease-expiry-only recovery
# model as Phase 5 -- no heartbeat/crash-detection logic).
APPLICATION_LEASE_SECONDS = _env_int("APPLICATION_LEASE_SECONDS", 300)
APPLICATION_WORKER_CONCURRENCY = _env_int("APPLICATION_WORKER_CONCURRENCY", 2)

# ANALYZE / ASSIST / AUTO_PERMITTED. ASSIST remains the default -- see
# CLAUDE.md Phase 8 section 3 ("ASSIST remains default").
APPLICATION_DEFAULT_MODE = os.getenv("APPLICATION_DEFAULT_MODE", "ASSIST").strip().upper() or "ASSIST"

# --- Phase 9: production application-worker fleet ---------------------------
# See docs/phase9-production-application-workers.md,
# docs/application-worker-architecture.md.
#
# Safe defaults: OFF until the user explicitly opts in. Neither flag below
# changes anything about APPLICATION_EXECUTOR_ENABLED/AUTO_SUBMIT_ENABLED
# above (CLAUDE.md Phase 9 section 12) -- they only gate whether the
# scheduler/worker loop runs at all.

# CLAUDE.md Phase 9 section 37: AUTO_PREPARE (auto-queue+prepare eligible
# jobs) is independent of AUTO_SUBMIT_ENABLED (submit permission). The
# scheduler (app.applications.scheduler) only ever queues in ASSIST mode
# unless AUTO_SUBMIT_ENABLED is ALSO true -- see
# app.applications.scheduler.run_cycle().
APPLICATION_AUTO_PREPARE_ENABLED = _env_bool("APPLICATION_AUTO_PREPARE_ENABLED", False)
APPLICATION_SCHEDULER_MAX_QUEUE_PER_CYCLE = _env_int("APPLICATION_SCHEDULER_MAX_QUEUE_PER_CYCLE", 5)
APPLICATION_SCHEDULER_CYCLE_SECONDS = _env_int("APPLICATION_SCHEDULER_CYCLE_SECONDS", 300)

# Application worker daemon (app.applications.worker) -- mirrors the Phase 5
# discovery-worker settings above, but deliberately far more conservative:
# application submission is rare, high-consequence, and rate-limited, so it
# is never treated like bulk job-discovery polling (CLAUDE.md Phase 9
# section 35 "no spray-and-pray").
APPLICATION_WORKER_HEARTBEAT_SECONDS = _env_int("APPLICATION_WORKER_HEARTBEAT_SECONDS", 15)
APPLICATION_WORKER_IDLE_SLEEP_SECONDS = _env_float("APPLICATION_WORKER_IDLE_SLEEP_SECONDS", 10.0)
APPLICATION_WORKER_CYCLE_TIME_BUDGET_SECONDS = _env_int("APPLICATION_WORKER_CYCLE_TIME_BUDGET_SECONDS", 60)
APPLICATION_MAX_EXECUTIONS_PER_WORKER_CYCLE = _env_int("APPLICATION_MAX_EXECUTIONS_PER_WORKER_CYCLE", 5)
APPLICATION_WORKER_SHUTDOWN_GRACE_SECONDS = _env_int("APPLICATION_WORKER_SHUTDOWN_GRACE_SECONDS", 30)
APPLICATION_SUPERVISOR_MAX_WORKERS = _env_int("APPLICATION_SUPERVISOR_MAX_WORKERS", 4)
# A claimed-but-skipped item (circuit open / provider at its concurrency
# limit) gets a short cooldown extension rather than a bare release --
# same busy-spin-avoidance rationale as Phase 5 section 29/CLAUDE.md Phase 8
# section 40.
APPLICATION_SKIP_COOLDOWN_SECONDS = _env_int("APPLICATION_SKIP_COOLDOWN_SECONDS", 10)

# Per-provider submission concurrency -- deliberately tiny (never treated
# like discovery's PROVIDER_CONCURRENCY_DEFAULT=3+) since a real submission
# in flight is a consequential, rate-limited action, not a cheap GET.
APPLICATION_PROVIDER_CONCURRENCY_DEFAULT = _env_int("APPLICATION_PROVIDER_CONCURRENCY_DEFAULT", 1)

# Submission circuit breaker (app.applications.circuit) -- separate state
# from the discovery circuit breaker (app.workers.circuit), tripped far more
# conservatively: fewer consecutive failures, longer cooldown, since a
# submission failure is more consequential than a discovery poll failure.
APPLICATION_CIRCUIT_BREAKER_FAILURE_THRESHOLD = _env_float("APPLICATION_CIRCUIT_BREAKER_FAILURE_THRESHOLD", 0.5)
APPLICATION_CIRCUIT_BREAKER_COOLDOWN_SECONDS = _env_int("APPLICATION_CIRCUIT_BREAKER_COOLDOWN_SECONDS", 300)
APPLICATION_CIRCUIT_CONSECUTIVE_TRIP_THRESHOLD = _env_int("APPLICATION_CIRCUIT_CONSECUTIVE_TRIP_THRESHOLD", 3)

# Dead-lettering for the application queue -- CONSECUTIVE PERMANENT failures
# only (never transient), same distinction as Phase 5's DEAD_LETTER_MAX_ATTEMPTS.
APPLICATION_DEAD_LETTER_MAX_ATTEMPTS = _env_int("APPLICATION_DEAD_LETTER_MAX_ATTEMPTS", 3)

# Reconciliation worker (app.applications.reconcile_worker) -- an automated
# EVIDENCE-GATHERING pass only; it never itself marks an execution APPLIED
# without genuine provider-side evidence (CLAUDE.md Phase 9 section 8), and
# app.applications.reconcile.reconcile_execution() remains the only function
# that actually changes a SUBMISSION_STATUS_UNKNOWN execution's status.
RECONCILE_WORKER_ENABLED = _env_bool("RECONCILE_WORKER_ENABLED", False)
RECONCILE_WORKER_INTERVAL_SECONDS = _env_int("RECONCILE_WORKER_INTERVAL_SECONDS", 900)

# Browser-assist (CLAUDE.md Phase 9 sections 21-22) -- optional, Playwright-
# backed, visible-browser-only preparation aid. Off by default; requires the
# `playwright` package AND its browser binaries to be installed separately
# (`playwright install chromium`), never a default/implicit test dependency.
BROWSER_ASSIST_ENABLED = _env_bool("BROWSER_ASSIST_ENABLED", False)
BROWSER_ASSIST_HEADLESS = _env_bool("BROWSER_ASSIST_HEADLESS", False)
BROWSER_ASSIST_TIMEOUT_SECONDS = _env_int("BROWSER_ASSIST_TIMEOUT_SECONDS", 30)
BROWSER_ASSIST_PROFILE_DIR = DATA_DIR / "browser_assist_runtime"

# --- Phase 10: production-quality real-ATS browser assist -------------------
# See docs/phase10-real-ats-assist.md, docs/browser-assist-sessions.md.
# BROWSER_HEADLESS is the Phase 10 name for the same concept as
# BROWSER_ASSIST_HEADLESS above (CLAUDE.md Phase 10 section 65) -- both are
# read; BROWSER_HEADLESS wins if both are set, so existing .env files using
# the Phase 9 name keep working unchanged.
BROWSER_HEADLESS = _env_bool("BROWSER_HEADLESS", BROWSER_ASSIST_HEADLESS)
# Browser sessions are expensive and interactive -- bounded, conservative
# concurrency (CLAUDE.md Phase 10 section 45), never treated like discovery's
# multi-worker-per-provider concurrency.
BROWSER_ASSIST_CONCURRENCY = _env_int("BROWSER_ASSIST_CONCURRENCY", 1)
# A session with no activity for this long is reaped as EXPIRED (CLAUDE.md
# Phase 10 section 50) -- never auto-submits or deletes evidence, only marks
# the session/lease so a fresh attempt can start cleanly.
BROWSER_SESSION_TIMEOUT_MINUTES = _env_int("BROWSER_SESSION_TIMEOUT_MINUTES", 30)
# Same directory as BROWSER_ASSIST_PROFILE_DIR (Phase 9 section 5's "runtime
# paths, never persisted longer than needed") -- Phase 10 name kept alongside
# for the config section CLAUDE.md's Phase 10 build brief explicitly names.
BROWSER_RUNTIME_DIR = Path(os.getenv("BROWSER_RUNTIME_DIR", "") or str(BROWSER_ASSIST_PROFILE_DIR))
# How long a claimed browser-session lease is held before another
# assist-capable worker/dashboard action may reclaim it (CLAUDE.md Phase 10
# section 63) -- same lease-expiry-only recovery model as every other queue
# in this project, no heartbeat/crash-detection logic.
BROWSER_SESSION_LEASE_SECONDS = _env_int("BROWSER_SESSION_LEASE_SECONDS", 600)

# --- Phase 11: real ATS flow hardening ---------------------------------------
# See docs/phase11-ats-flow-hardening.md.
#
# Whether resume_session() is allowed to safely reopen a fresh browser at a
# session's saved application_url when the original browser/process is gone
# (CLAUDE.md Phase 11 section 25 "reconstruct-and-resume, not process
# reattachment"). True by default -- this is the SAME safe fallback Phase 10
# already implemented unconditionally; the flag exists so an operator can
# force AWAITING/pre-submission sessions to require an explicit human restart
# instead, without touching code.
BROWSER_SESSION_RECONSTRUCT_ENABLED = _env_bool("BROWSER_SESSION_RECONSTRUCT_ENABLED", True)

# How many days a LIVE_PUBLIC capability-evidence observation stays fresh
# before app.applications.capability_evidence.is_stale() flags it for
# revalidation (CLAUDE.md Phase 11 section 43) -- never auto-disables the
# capability, only surfaces it in the doctor/dashboard.
CAPABILITY_EVIDENCE_MAX_AGE_DAYS = _env_int("CAPABILITY_EVIDENCE_MAX_AGE_DAYS", 30)

# Gates scripts/phase11_live_validation.py's actual network+browser
# validation run (CLAUDE.md Phase 11 sections 47-49) -- off by default, same
# "never a default or implicit dependency" principle as BROWSER_ASSIST_ENABLED.
# Does not affect pytest, which never requires network/browser by default.
REAL_ATS_VALIDATION_ENABLED = _env_bool("REAL_ATS_VALIDATION_ENABLED", False)

# --- Phase 12: SPA/dynamic ATS flow hardening --------------------------------
# See docs/phase12-spa-ats-hardening.md, docs/spa-application-navigation.md.
#
# Bounded DOM-stabilization wait (CLAUDE.md Phase 12 sections 11-12): used
# instead of blindly trusting Playwright's `networkidle` load state, which a
# genuinely single-page-app site may never reach (it can keep issuing
# background XHR/websocket traffic indefinitely). Polls for recognizable
# form content OR a login/CAPTCHA wall OR the DOM settling (unchanged across
# consecutive polls) OR this timeout -- whichever comes first.
BROWSER_DOM_STABILIZATION_TIMEOUT_MS = _env_int("BROWSER_DOM_STABILIZATION_TIMEOUT_MS", 8000)
BROWSER_DOM_STABILIZATION_POLL_MS = _env_int("BROWSER_DOM_STABILIZATION_POLL_MS", 250)
# Consecutive identical-signature polls before the DOM is considered
# "settled" even when no recognizable form content ever appeared.
BROWSER_DOM_STABILIZATION_SETTLE_POLLS = _env_int("BROWSER_DOM_STABILIZATION_SETTLE_POLLS", 3)

# --- Phase 13: provider resilience and real-world ATS reliability -----------
# See docs/phase13-provider-resilience.md.
#
# CLAUDE.md Phase 13 sections 13-14, 56: safe, read-only application-flow
# canary validation. Off by default, same "never enabled automatically"
# principle as every other real-network/browser flag in this project --
# app.applications.canary.run_scheduled_canaries() refuses to do anything
# while this is false. Does not affect pytest, which never requires
# network/browser by default.
REAL_ATS_CANARY_ENABLED = _env_bool("REAL_ATS_CANARY_ENABLED", False)
# Conservative floor on how often a scheduled canary may revisit the SAME
# target -- read by the (not-yet-wired-to-any-cron) scheduling helper, never
# bypassed even when REAL_ATS_CANARY_ENABLED is true, so an operator can't
# accidentally configure a hammering loop.
REAL_ATS_CANARY_INTERVAL_HOURS = _env_int("REAL_ATS_CANARY_INTERVAL_HOURS", 24)

# CLAUDE.md Phase 13 section 82: same underlying staleness window as
# CAPABILITY_EVIDENCE_MAX_AGE_DAYS (Phase 11) -- kept as an explicit alias
# name too since this phase's build brief names it separately, but both read
# the identical env var so there is exactly one number an operator has to
# set, never two that could silently drift apart.
PROVIDER_CAPABILITY_MAX_AGE_DAYS = CAPABILITY_EVIDENCE_MAX_AGE_DAYS

# CLAUDE.md Phase 13 section 4: whether the formal job-identity gate
# (app.applications.job_identity.verify_job_identity_full) is consulted at
# all before a real ATS resume upload / READY_FOR_FINAL_SUBMIT transition.
# True by default -- an operator can only ever turn this OFF explicitly, it
# is never off by silent default the way a brand-new optional feature would
# be, since job-identity safety is a core Phase 13 objective, not an add-on.
APPLICATION_IDENTITY_REQUIRED = _env_bool("APPLICATION_IDENTITY_REQUIRED", True)
# CLAUDE.md Phase 13 acceptance correction: the minimum JobIdentityVerdict
# (by the VERIFIED > PROBABLE > AMBIGUOUS > INSUFFICIENT ordering,
# app.applications.job_identity._VERDICT_RANK) that may pass the pre-
# upload/pre-final-submit gate WITHOUT pausing for review. Defaults to
# "VERIFIED" -- only a verdict backed by 2+ independent corroborating
# signals (or a matching requisition id) may continue unattended;
# PROBABLE/AMBIGUOUS/INSUFFICIENT all pause (PAUSED_JOB_IDENTITY_UNVERIFIED)
# by default. An operator may explicitly loosen this (e.g. to "PROBABLE")
# to accept single-signal corroboration as sufficient -- a deliberate,
# documented risk acceptance, never the silent default. MISMATCH (a
# CONFIRMED contradiction) is never affected by this setting -- it always
# pauses (PAUSED_JOB_IDENTITY_MISMATCH) unconditionally.
APPLICATION_IDENTITY_MIN_CONFIDENCE = (
    os.getenv("APPLICATION_IDENTITY_MIN_CONFIDENCE", "VERIFIED").strip().upper() or "VERIFIED"
)

# CLAUDE.md Phase 13 section 35: bounded retry for ordinary SPA/form
# discovery -- never an infinite loop. Distinct from
# APPLICATION_CIRCUIT_CONSECUTIVE_TRIP_THRESHOLD (submission circuit) and
# app.workers.circuit's discovery threshold -- this one bounds a single
# assist provider's DOM-discovery retry attempts within one pass.
ASSIST_PROVIDER_MAX_RETRIES = _env_int("ASSIST_PROVIDER_MAX_RETRIES", 2)

# =============================================================================
# CLAUDE.md Phase 14: resume optimizer / unified dashboard.
# =============================================================================

# Whether jobs are queued for background resume optimization automatically
# (CLAUDE.md section 56). False by default -- generation always remains
# available synchronously via the dashboard "Generate/Regenerate Resume"
# action and the CLI regardless of this flag, mirroring
# APPLICATION_AUTO_PREPARE_ENABLED's "never gate manual generation" contract.
RESUME_OPTIMIZATION_ENABLED = _env_bool("RESUME_OPTIMIZATION_ENABLED", False)
RESUME_OPTIMIZATION_INTERVAL_SECONDS = _env_int("RESUME_OPTIMIZATION_INTERVAL_SECONDS", 300)
RESUME_OPTIMIZATION_BATCH_SIZE = _env_int("RESUME_OPTIMIZATION_BATCH_SIZE", 5)

# =============================================================================
# CLAUDE.md Phase 15: dashboard result bounding.
# =============================================================================

# =============================================================================
# One-click autonomous agent orchestrator (app/agent/orchestrator.py) and the
# one-page resume hard output contract (app/resume_optimizer/one_page.py).
# =============================================================================

# How often the orchestrator's cycle (discovery -> resume -> auto-prepare ->
# execute) repeats while the agent is RUNNING. AGENT_INTERVAL_MINUTES is the
# name this build brief uses; falls back to the existing
# DISCOVERY_INTERVAL_MINUTES (unchanged Phase 2 default of 15) so a single
# number controls both unless an operator deliberately sets them differently.
AGENT_INTERVAL_MINUTES = _env_int("AGENT_INTERVAL_MINUTES", DISCOVERY_INTERVAL_MINUTES)

# Production-v2 watchdog: how stale agent_run_state.heartbeat_at may get
# while actual_state == RUNNING before the dashboard/doctor treat it as a
# possible stuck cycle (never auto-killed -- see app/agent/doctor.py; a
# passive warning only, since forcibly cancelling an in-flight sync stage
# mid-network-call is not something Python can do safely).
AGENT_HEARTBEAT_STALE_SECONDS = _env_int("AGENT_HEARTBEAT_STALE_SECONDS", 300)

# Single-orchestrator-guarantee safety net (autonomous-core-v3 hardening):
# the orchestrator is designed to run as one lightweight single-process
# asyncio loop per this project's existing scheduler convention (matching
# app.applications.background_scheduler / app.resume_optimizer.scheduler --
# deliberately NOT a distributed/leased worker capability). This lease is a
# defensive guard against the operational mistake of accidentally starting a
# second process against the same database, not a distributed-control-plane
# feature: only the instance holding the lease ever runs cycle stages,
# renewed on every heartbeat, self-healing via plain expiry (never a
# heartbeat-based "is the other process alive" check) if the lease holder
# crashes without releasing it. STOP AGENT only stops the loop in the
# process that receives the request -- see docs/one-click-agent.md.
AGENT_ORCHESTRATOR_LEASE_SECONDS = _env_int("AGENT_ORCHESTRATOR_LEASE_SECONDS", 120)

# Sponsorship policy (CLAUDE.md production-v2 section 12). Neither value ever
# redefines LIKELY_SPONSOR as confirmed, and neither ever changes the
# executor's own CONFIRMED_SPONSOR-only auto-submit gate (app.applications.
# eligibility already hard-codes auto_submit_eligible = CONFIRMED_SPONSOR
# only, independent of this flag). This setting only controls whether the
# orchestrator's auto-prepare stage (resume + application package
# generation) also runs for LIKELY_SPONSOR jobs:
#   CONFIRMED_OR_LIKELY_WITH_REVIEW (default): auto-prepare runs for both
#     CONFIRMED_SPONSOR and LIKELY_SPONSOR -- this is the existing,
#     already-tested Phase 14/15 behavior (LIKELY jobs land on
#     REVIEW_REQUIRED, package generated for human review, never
#     auto-submitted), kept as the default rather than silently narrowed.
#   CONFIRMED_ONLY: the orchestrator's auto-prepare stage skips
#     LIKELY_SPONSOR jobs entirely (manual Regenerate Resume still works).
SPONSORSHIP_POLICY = (
    os.getenv("SPONSORSHIP_POLICY", "CONFIRMED_OR_LIKELY_WITH_REVIEW").strip().upper()
    or "CONFIRMED_OR_LIKELY_WITH_REVIEW"
)

# Every automatically-generated job-specific resume must render as exactly
# one PDF page. True by default -- this is a hard output contract, not an
# optional nicety (see docs/one-page-resume-contract.md).
ONE_PAGE_RESUME_REQUIRED = _env_bool("ONE_PAGE_RESUME_REQUIRED", True)
# Bounded compression ladder (CLAUDE.md one-click-agent section 8): never
# shrink font below this size, never take more than this many compression
# steps before giving up honestly (ResumeVariantStatus.REVIEW_REQUIRED)
# rather than producing a tiny unreadable one-page render.
ONE_PAGE_MIN_FONT_SIZE = _env_float("ONE_PAGE_MIN_FONT_SIZE", 9.5)
ONE_PAGE_MAX_COMPRESSION_STEPS = _env_int("ONE_PAGE_MAX_COMPRESSION_STEPS", 8)

# Minimum resume_optimizer internal_alignment_score (0-100, NOT an ATS score
# or interview probability -- see app.resume_optimizer.quality) required for
# a job to be automatically prepared/queued by the orchestrator. Distinct
# from MIN_APPLICATION_MATCH_SCORE (the older, keyword-based technical match
# score) -- both gates apply.
MIN_ALIGNMENT_FOR_AUTO_PREPARE = _env_int("MIN_ALIGNMENT_FOR_AUTO_PREPARE", 40)

# Budget/daily-cap config the orchestrator's own cycle additionally respects
# (on top of the existing MAX_APPLICATIONS_PER_HOUR/_PER_DAY/
# _PER_COMPANY_PER_DAY rate limits enforced by app.applications.rate_limit,
# which are absolute safety limits, not orchestrator-cycle pacing). These are
# deliberately never called "interview probability" or similar.
MAX_RESUMES_PER_CYCLE = _env_int("MAX_RESUMES_PER_CYCLE", 10)
MAX_APPLICATIONS_PER_CYCLE = _env_int("MAX_APPLICATIONS_PER_CYCLE", 5)

# --- Greenhouse Verified Submission Contract V1 -----------------------------
# See docs/greenhouse-verified-submission-contract-v1.md.
#
# The CONTROLLED CANARY gate: off by default, matching every other real-
# network/real-browser flag in this project (BROWSER_ASSIST_ENABLED,
# REAL_ATS_CANARY_ENABLED). Even when true, `app.applications.greenhouse_canary`
# additionally requires an explicit `confirm=True` on the specific call and a
# current, verified, ACTIVE durable approval for the specific job -- this
# flag alone never authorizes anything. No test in this project may set this
# to True; tests exercise `app.applications.greenhouse_submit_engine`
# directly against local `file://` fixtures instead.
GREENHOUSE_SUBMIT_CANARY_ENABLED = _env_bool("GREENHOUSE_SUBMIT_CANARY_ENABLED", False)
# Bounded wait for a genuine Playwright click on the identified FINAL_SUBMIT
# control (app.applications.greenhouse_submit_engine._click_and_observe). A
# disabled/unresponsive control raises a real Playwright TimeoutError before
# this elapses -- the honest "no click was ever dispatched" signal used to
# distinguish a pre-click timeout from a post-click one.
GREENHOUSE_SUBMIT_CLICK_TIMEOUT_MS = _env_int("GREENHOUSE_SUBMIT_CLICK_TIMEOUT_MS", 10000)

# CLAUDE.md Phase 15 section 42/44: a Phase 15 large-state benchmark
# (scripts/phase15_release_benchmark.py) measured the unified dashboard's
# rendered pipeline table growing unboundedly with total job count -- 24MB
# of HTML / 5.7s render at 50,000 synthetic jobs. Summary card counts
# (app.pipeline_dashboard.compute_pipeline_summary) still scan every job --
# that's a deliberate, documented Phase 14 tradeoff for this project's
# realistic single-user scale (see that function's own docstring) and is
# NOT capped here. This constant only bounds the actual rendered table rows
# (already sorted by priority_score DESC, so capping keeps the top-N most
# relevant/actionable jobs) -- the dashboard shows a "showing top N of M"
# note when the true matching count exceeds this.
DASHBOARD_MAX_TABLE_ROWS = _env_int("DASHBOARD_MAX_TABLE_ROWS", 500)
