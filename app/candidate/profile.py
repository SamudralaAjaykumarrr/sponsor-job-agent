import json
from pathlib import Path

from app.candidate.schema import CandidateProfile
from app.config import CANDIDATE_DIR

PROFILE_PATH = CANDIDATE_DIR / "profile.json"

_README = """This directory holds PRIVATE candidate facts used to generate truthful,
verified resumes and application answers. Nothing here is committed to git.

Edit profile.json and replace every "NEEDS_USER_INPUT" value with your real,
verifiable information. Only list skills/experience you can actually back up --
the resume generator will only ever claim what is written here.
"""


def _write_readme() -> None:
    readme_path = CANDIDATE_DIR / "README.txt"
    if not readme_path.exists():
        readme_path.write_text(_README)


def ensure_profile_exists() -> Path:
    _write_readme()
    if not PROFILE_PATH.exists():
        blank = CandidateProfile()
        PROFILE_PATH.write_text(blank.model_dump_json(indent=2))
    return PROFILE_PATH


def load_profile() -> CandidateProfile:
    ensure_profile_exists()
    raw = json.loads(PROFILE_PATH.read_text())
    return CandidateProfile.model_validate(raw)


def save_profile(profile: CandidateProfile) -> None:
    PROFILE_PATH.write_text(profile.model_dump_json(indent=2))


def missing_fields(profile: CandidateProfile) -> list[str]:
    """Return dotted-path list of fields still set to NEEDS_USER_INPUT or unset."""
    missing: list[str] = []
    data = profile.model_dump()

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        else:
            if node == "NEEDS_USER_INPUT" or node is None:
                missing.append(path)

    walk(data, "")
    return missing
