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
