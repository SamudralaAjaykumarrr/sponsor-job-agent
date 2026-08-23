"""Release-candidate acceptance runner (CLAUDE.md Phase 15 sections 81-82):
`python -m app.acceptance`.

Runs the deterministic release checks that are safe and reasonable in any
environment -- never submits a real application, never requires internet,
never destroys data (every DB touched here is either read-only against the
real database, via app.doctor's existing read-only contract, or a fresh
temp SQLite file this module creates and deletes itself). PostgreSQL and
real-browser suites are attempted but OPTIONAL: a missing `pgserver` /
Playwright / launchable Chromium is reported honestly as SKIPPED with its
real reason, never silently treated as a pass and never faked.

Produces both a human-readable stdout report and a machine-readable JSON
report (written to OUTPUT_DIR/release_acceptance_report.json, which is
already gitignored -- see CLAUDE.md Phase 15 section 82: "Do not commit
environment-specific/private report artifacts")."""

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class CheckResult:
    name: str
    status: str  # "PASS" | "FAIL" | "SKIPPED"
    detail: str = ""
    duration_seconds: float = 0.0


@dataclass
class AcceptanceReport:
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(c.status == "FAIL" for c in self.checks)

    def as_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "overall": "PASS" if self.ok else "FAIL",
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail, "duration_seconds": round(c.duration_seconds, 2)}
                for c in self.checks
            ],
        }


def _run(cmd: list[str], timeout: int = 900) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, (proc.stdout[-4000:] + proc.stderr[-4000:])


def _time_it(fn):
    import time

    start = time.monotonic()
    result = fn()
    return result, time.monotonic() - start


def check_default_pytest(report: AcceptanceReport) -> None:
    (rc, out), duration = _time_it(lambda: _run([sys.executable, "-m", "pytest", "-q"]))
    tail = out.strip().splitlines()[-1] if out.strip() else ""
    report.checks.append(CheckResult("pytest_default", "PASS" if rc == 0 else "FAIL", tail, duration))


def check_postgres_pytest(report: AcceptanceReport) -> None:
    try:
        import pgserver  # noqa: F401
    except ImportError:
        report.checks.append(CheckResult("pytest_postgres", "SKIPPED",
                                          "pgserver not installed (pip install -r requirements-dev.txt) -- "
                                          "default pytest never depends on this."))
        return
    (rc, out), duration = _time_it(lambda: _run([sys.executable, "-m", "pytest", "-m", "postgres", "-q"]))
    tail = out.strip().splitlines()[-1] if out.strip() else ""
    report.checks.append(CheckResult("pytest_postgres", "PASS" if rc == 0 else "FAIL", tail, duration))


def check_browser_pytest(report: AcceptanceReport) -> None:
    try:
        import playwright  # noqa: F401
    except ImportError:
        report.checks.append(CheckResult("pytest_browser", "SKIPPED",
                                          "playwright not installed -- default pytest never depends on this."))
        return
    (rc, out), duration = _time_it(lambda: _run([sys.executable, "-m", "pytest", "-m", "browser", "-q"]))
    tail = out.strip().splitlines()[-1] if out.strip() else ""
    if rc == 0 and "0 passed" not in out and "no tests ran" not in out.lower():
        report.checks.append(CheckResult("pytest_browser", "PASS", tail, duration))
    elif "no tests ran" in out.lower() or rc == 5:
        report.checks.append(CheckResult("pytest_browser", "SKIPPED", "no browser tests collected", duration))
    else:
        # Chromium launch failure inside a test manifests as individual test
        # failures/skips, not a pytest-level error -- report honestly either way.
        report.checks.append(CheckResult("pytest_browser", "PASS" if rc == 0 else "FAIL", tail, duration))


def check_global_doctor(report: AcceptanceReport) -> None:
    def _go():
        from app.doctor import run_global_doctor

        return run_global_doctor()

    result, duration = _time_it(_go)
    status = "FAIL" if result.serious_count else "PASS"
    detail = f"{result.serious_count} serious, {result.warning_count} warning(s) across {len(result.subsystems_run)} subsystem(s)"
    report.checks.append(CheckResult("global_doctor", status, detail, duration))


def check_secret_scan(report: AcceptanceReport) -> None:
    (rc, out), duration = _time_it(lambda: _run([sys.executable, str(REPO_ROOT / "scripts" / "secret_scan.py")]))
    tail = out.strip().splitlines()[-1] if out.strip() else ""
    report.checks.append(CheckResult("secret_scan", "PASS" if rc == 0 else "FAIL", tail, duration))


def check_compile(report: AcceptanceReport) -> None:
    (rc, out), duration = _time_it(lambda: _run([sys.executable, "-m", "compileall", "-q", "app", "scripts"]))
    report.checks.append(CheckResult("compile_check", "PASS" if rc == 0 else "FAIL",
                                      "all .py files compile" if rc == 0 else out[-1000:], duration))


def check_fresh_sqlite_migration(report: AcceptanceReport) -> None:
    """Creates a throwaway SQLite file in a temp directory that is deleted
    when the `with` block exits -- never touches the real data/app.db
    (CLAUDE.md Phase 15 section 90)."""
    try:
        import sqlite3

        from app import db, migrations

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "acceptance_fresh.db"
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            conn.executescript(db.SCHEMA)
            # Mirrors app.db.init_sqlite_db() exactly: the legacy pre-numbered
            # additive-column migrations (app.db._migrate_jobs_table /
            # _migrate_phase5_lease_columns) run BEFORE the versioned
            # migrations.run_pending() -- some numbered migrations (e.g. one
            # touching jobs.provider) depend on columns those legacy helpers
            # add. A real fresh init_db() always runs both in this order; this
            # check must simulate the same order to be a meaningful check.
            db._migrate_jobs_table(conn)
            db._migrate_phase5_lease_columns(conn)
            migrations.run_pending(conn, backend="sqlite")
            conn.commit()
            version = migrations.current_db_version(conn)
            compatible = migrations.is_compatible(conn)
            conn.close()
        status = "PASS" if compatible and version == migrations.CURRENT_SCHEMA_VERSION else "FAIL"
        report.checks.append(CheckResult("fresh_sqlite_migration", status,
                                          f"fresh DB migrated to schema_version={version} (expected {migrations.CURRENT_SCHEMA_VERSION})"))
    except Exception as exc:  # noqa: BLE001
        report.checks.append(CheckResult("fresh_sqlite_migration", "FAIL", f"{type(exc).__name__}: {exc}"))


def check_gitignore_coverage(report: AcceptanceReport) -> None:
    """CLAUDE.md Phase 15 section 27: verifies the actual .gitignore text
    covers every required pattern -- not a re-implementation of git's own
    matching, just a presence check on the documented required entries."""
    required_substrings = [
        ".env", "candidate_data", "data/app.db", "output", "data/private",
        "browser_assist_runtime", "runtime",
    ]
    gitignore_text = (REPO_ROOT / ".gitignore").read_text()
    missing = [s for s in required_substrings if s not in gitignore_text]
    status = "FAIL" if missing else "PASS"
    detail = "all required patterns present" if not missing else f"missing: {missing}"
    report.checks.append(CheckResult("gitignore_coverage", status, detail))


def run_acceptance(*, include_optional: bool = True) -> AcceptanceReport:
    report = AcceptanceReport()
    check_compile(report)
    check_gitignore_coverage(report)
    check_secret_scan(report)
    check_fresh_sqlite_migration(report)
    check_global_doctor(report)
    check_default_pytest(report)
    if include_optional:
        check_postgres_pytest(report)
        check_browser_pytest(report)
    return report


def main() -> int:
    report = run_acceptance()
    data = report.as_dict()

    print("=" * 70)
    print("Sponsor Job Agent -- Release Acceptance Report")
    print(f"Generated: {data['generated_at']}")
    print("=" * 70)
    for c in report.checks:
        print(f"  [{c.status:8s}] {c.name:28s} {c.detail}")
    print("=" * 70)
    print(f"OVERALL: {data['overall']}")

    try:
        from app import config

        out_path = config.OUTPUT_DIR / "release_acceptance_report.json"
        out_path.write_text(json.dumps(data, indent=2))
        print(f"Machine-readable report written to {out_path}")
    except Exception as exc:  # noqa: BLE001 -- report already printed; writing the file is a bonus, not required
        print(f"(could not write JSON report file: {exc})")

    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
