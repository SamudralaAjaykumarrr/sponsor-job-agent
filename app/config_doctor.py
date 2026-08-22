"""Configuration validation ("config doctor" -- CLAUDE.md Phase 15 sections
11-13). Read-only: reports problems, never mutates config or the
environment. Never prints a secret value (a DATABASE_URL password, if any,
is always redacted before being included in a detail string).

This is deliberately a plain function module, not a class hierarchy --
config validation is a one-shot, stateless pass over already-loaded
app.config values, matching the read-only "doctor" contract used by
app.registry.doctor / app.sponsorship.doctor / app.applications.doctor /
app.resume_optimizer.doctor (CLAUDE.md Phase 15 section 10: "reuse existing
doctors rather than duplicating logic")."""

import os
import re
from dataclasses import dataclass, field

from app import config


@dataclass
class Issue:
    severity: str  # "serious" | "warning"
    check: str
    detail: str


@dataclass
class ConfigDoctorReport:
    issues: list[Issue] = field(default_factory=list)

    @property
    def serious_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "serious")

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "warning")

    def as_dict(self) -> dict:
        return {
            "serious_count": self.serious_count,
            "warning_count": self.warning_count,
            "issues": [{"severity": i.severity, "check": i.check, "detail": i.detail} for i in self.issues],
        }


def _redact_database_url(url: str) -> str:
    """Never echo a DSN's credentials -- same redaction contract as
    app.health.check_readiness()'s "never leaks DB credentials"."""
    return re.sub(r"://([^:/@]+):([^@/]+)@", r"://\1:***@", url)


def _check_database_url(report: ConfigDoctorReport) -> None:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        report.issues.append(Issue("warning", "database_url_unset",
                                    "DATABASE_URL is unset -- defaulting to local SQLite (data/app.db). "
                                    "Fine for LOCAL_DEVELOPMENT; set a postgres:// URL for PRODUCTION."))
        return
    if not (url.startswith("postgres://") or url.startswith("postgresql://")):
        report.issues.append(Issue("serious", "database_url_unrecognized",
                                    f"DATABASE_URL is set but not a recognized postgres(ql):// URL "
                                    f"({_redact_database_url(url)}) -- app.db only supports SQLite-default "
                                    "or a postgres(ql):// DSN."))


def _check_dangerous_defaults(report: ConfigDoctorReport) -> None:
    """CLAUDE.md Phase 15 section 13: 'keep dangerous capabilities disabled
    by default' -- this doesn't forbid an operator from turning them on, it
    only flags the combination truthfully so it's never accidentally
    invisible in a startup summary or deployment review."""
    if config.AUTO_SUBMIT_ENABLED:
        report.issues.append(Issue("warning", "auto_submit_enabled",
                                    "AUTO_SUBMIT_ENABLED=true -- automated submission is live. Confirm this is "
                                    "an intentional, reviewed decision, not a default carried over from an example."))
    if config.AUTO_SUBMIT_ENABLED and not config.APPLICATION_EXECUTOR_ENABLED:
        report.issues.append(Issue("serious", "auto_submit_without_executor",
                                    "AUTO_SUBMIT_ENABLED=true but APPLICATION_EXECUTOR_ENABLED=false -- auto-submit "
                                    "can never actually run without the executor; likely a misconfiguration."))
    if config.REAL_ATS_CANARY_ENABLED and not config.BROWSER_ASSIST_ENABLED:
        report.issues.append(Issue("warning", "canary_without_browser_assist",
                                    "REAL_ATS_CANARY_ENABLED=true but BROWSER_ASSIST_ENABLED=false -- canaries "
                                    "use the same browser runtime as browser-assist and will have nothing to do."))


def _check_runtime_directories(report: ConfigDoctorReport) -> None:
    for name, path in (
        ("DATA_DIR", config.DATA_DIR),
        ("OUTPUT_DIR", config.OUTPUT_DIR),
        ("CANDIDATE_DIR", config.CANDIDATE_DIR),
    ):
        if not path.exists():
            report.issues.append(Issue("serious", "missing_runtime_directory", f"{name} ({path}) does not exist."))
        elif not os.access(path, os.W_OK):
            report.issues.append(Issue("serious", "unwritable_runtime_directory", f"{name} ({path}) is not writable."))


def _check_rate_limit_sanity(report: ConfigDoctorReport) -> None:
    """A per-company-per-day cap greater than the per-day cap, or a per-hour
    cap greater than the per-day cap, can never actually bind -- almost
    certainly a typo'd .env value, not a deliberate choice."""
    if config.MAX_APPLICATIONS_PER_COMPANY_PER_DAY > config.MAX_APPLICATIONS_PER_DAY:
        report.issues.append(Issue("warning", "rate_limit_company_exceeds_daily",
                                    f"MAX_APPLICATIONS_PER_COMPANY_PER_DAY ({config.MAX_APPLICATIONS_PER_COMPANY_PER_DAY}) "
                                    f"> MAX_APPLICATIONS_PER_DAY ({config.MAX_APPLICATIONS_PER_DAY}) -- the daily cap "
                                    "always binds first, so the company cap can never actually apply."))
    if config.MAX_APPLICATIONS_PER_HOUR > config.MAX_APPLICATIONS_PER_DAY:
        report.issues.append(Issue("warning", "rate_limit_hourly_exceeds_daily",
                                    f"MAX_APPLICATIONS_PER_HOUR ({config.MAX_APPLICATIONS_PER_HOUR}) > "
                                    f"MAX_APPLICATIONS_PER_DAY ({config.MAX_APPLICATIONS_PER_DAY})."))


def _check_sharding_sanity(report: ConfigDoctorReport) -> None:
    if config.REGISTRY_SHARD_COUNT < 1:
        report.issues.append(Issue("serious", "invalid_shard_count",
                                    f"REGISTRY_SHARD_COUNT={config.REGISTRY_SHARD_COUNT} -- must be >= 1."))
    elif not (0 <= config.REGISTRY_SHARD_INDEX < config.REGISTRY_SHARD_COUNT):
        report.issues.append(Issue("serious", "invalid_shard_index",
                                    f"REGISTRY_SHARD_INDEX={config.REGISTRY_SHARD_INDEX} is out of range for "
                                    f"REGISTRY_SHARD_COUNT={config.REGISTRY_SHARD_COUNT}."))


def _check_identity_confidence_value(report: ConfigDoctorReport) -> None:
    valid = {"VERIFIED", "PROBABLE", "AMBIGUOUS", "INSUFFICIENT"}
    if config.APPLICATION_IDENTITY_MIN_CONFIDENCE not in valid:
        report.issues.append(Issue("serious", "invalid_identity_confidence",
                                    f"APPLICATION_IDENTITY_MIN_CONFIDENCE="
                                    f"'{config.APPLICATION_IDENTITY_MIN_CONFIDENCE}' is not one of {sorted(valid)}."))
    elif config.APPLICATION_IDENTITY_MIN_CONFIDENCE != "VERIFIED":
        report.issues.append(Issue("warning", "loosened_identity_confidence",
                                    f"APPLICATION_IDENTITY_MIN_CONFIDENCE="
                                    f"'{config.APPLICATION_IDENTITY_MIN_CONFIDENCE}' -- looser than the safe "
                                    "default 'VERIFIED'; confirm this is a deliberate, reviewed risk acceptance."))


def run_config_doctor() -> ConfigDoctorReport:
    report = ConfigDoctorReport()
    _check_database_url(report)
    _check_dangerous_defaults(report)
    _check_runtime_directories(report)
    _check_rate_limit_sanity(report)
    _check_sharding_sanity(report)
    _check_identity_confidence_value(report)
    return report
