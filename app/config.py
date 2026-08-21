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
