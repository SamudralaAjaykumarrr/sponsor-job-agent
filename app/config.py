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
