"""Premium UI Settings page: a small, deliberately narrow allowlist of
runtime-mutable agent tuning knobs, persisted in `app_settings`
(app/migrations.py::_m051_app_settings_table) and applied to the `config`
module via `setattr` -- the exact same live-override mechanism
`app.agent.orchestrator._apply_config_overrides` already uses for
APPLICATION_EXECUTOR_ENABLED/APPLICATION_AUTO_PREPARE_ENABLED.

Only a config attribute that is genuinely read as `config.X` (never a
`from app.config import X` name-imported binding, which a `setattr` on the
module can never affect) may be added to ALLOWED_SETTINGS -- otherwise
"Save" would be a silent no-op, which the premium UI brief explicitly
forbids. Verified live-effective read sites as of this writing:
  - AGENT_INTERVAL_MINUTES -> app/agent/orchestrator.py's own cycle loop
  - MAX_JOBS_PER_CYCLE     -> app/agent/cycle.py's per-provider fetch loop
  - FRESHNESS_MAX_DAYS     -> app/agent/cycle.py's discovery-time filter

Deliberately excluded: MIN_MATCH_SCORE (app/pipeline.py name-imports it,
so a runtime override would not take effect -- fixing that is a pipeline.py
gate change, out of scope here), ENABLED_PROVIDERS (Phase 3/4 registry vs.
legacy dual-path selection, too easy to misconfigure from a UI toggle), and
every safety-relevant flag (APPLICATION_EXECUTOR_ENABLED, AUTO_SUBMIT_ENABLED,
BROWSER_ASSIST_ENABLED, ...) which CLAUDE.md requires stay env-only /
"never silently enabled"."""

from datetime import datetime, timezone

from app import config
from app.db import db_session


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class SettingSpec:
    def __init__(self, key: str, config_attr: str, label: str, help_text: str, min_value: int, max_value: int):
        self.key = key
        self.config_attr = config_attr
        self.label = label
        self.help_text = help_text
        self.min_value = min_value
        self.max_value = max_value


ALLOWED_SETTINGS: dict[str, SettingSpec] = {
    "agent_interval_minutes": SettingSpec(
        "agent_interval_minutes", "AGENT_INTERVAL_MINUTES", "Agent poll interval (minutes)",
        "How often the agent runs a discovery/prep/apply cycle while RUNNING.", 1, 1440,
    ),
    "max_jobs_per_cycle": SettingSpec(
        "max_jobs_per_cycle", "MAX_JOBS_PER_CYCLE", "Max jobs fetched per cycle",
        "Upper bound on how many jobs a single discovery cycle pulls per provider.", 1, 500,
    ),
    "freshness_max_days": SettingSpec(
        "freshness_max_days", "FRESHNESS_MAX_DAYS", "Freshness cutoff (days)",
        "Jobs older than this (by published_at) are skipped at discovery time.", 1, 30,
    ),
}


def current_values() -> dict[str, int]:
    return {key: getattr(config, spec.config_attr) for key, spec in ALLOWED_SETTINGS.items()}


def load_overrides() -> dict[str, int]:
    with db_session() as conn:
        rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    return {r["key"]: int(r["value"]) for r in rows if r["key"] in ALLOWED_SETTINGS}


def apply_overrides_on_startup() -> None:
    """Called once from the FastAPI lifespan, after init_db()/migrations
    have run -- re-applies any previously-saved settings to `config` so a
    process restart doesn't silently revert to the .env defaults."""
    for key, value in load_overrides().items():
        setattr(config, ALLOWED_SETTINGS[key].config_attr, value)


def save_settings(values: dict[str, int]) -> list[str]:
    """Validates, persists, and immediately applies each provided setting.
    Returns a list of human-readable validation errors (empty on full
    success); any key that fails validation is left unchanged both in the
    DB and in `config`."""
    errors: list[str] = []
    to_apply: dict[str, int] = {}
    for key, raw_value in values.items():
        spec = ALLOWED_SETTINGS.get(key)
        if spec is None:
            continue
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            errors.append(f"{spec.label}: must be a whole number.")
            continue
        if not (spec.min_value <= value <= spec.max_value):
            errors.append(f"{spec.label}: must be between {spec.min_value} and {spec.max_value}.")
            continue
        to_apply[key] = value

    if not to_apply:
        return errors

    now = utcnow()
    with db_session() as conn:
        for key, value in to_apply.items():
            conn.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, str(value), now),
            )
    for key, value in to_apply.items():
        setattr(config, ALLOWED_SETTINGS[key].config_attr, value)
    return errors
