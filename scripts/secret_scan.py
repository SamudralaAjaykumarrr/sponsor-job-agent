#!/usr/bin/env python3
"""Deterministic local secret/private-artifact scanner (CLAUDE.md Phase 15
section 26). No external/paid service, no network call -- pure regex over
the files git actually tracks (`git ls-files`), so it reflects exactly what
would be pushed, not the whole working tree (an untracked `.env` is already
harmless as long as it stays untracked -- `git status`/`.gitignore` cover
that; this script's job is what's IN the repo).

Deliberately conservative and NOT a claim of perfect detection (Phase 15
section 26: "Avoid false claims of perfect secret detection") -- pattern-
based scanning cannot catch every possible secret shape or a well-obfuscated
one. Treat a clean run as "no obvious secret patterns found", not "proven
secret-free".

Usage:
    python scripts/secret_scan.py            # scan git-tracked files
    python scripts/secret_scan.py --all       # also scan untracked files (slower, more false positives)
"""

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Path patterns that should never be tracked at all, regardless of content --
# matches CLAUDE.md Phase 15 section 27's ignore-audit list.
FORBIDDEN_PATH_PATTERNS = [
    re.compile(r"(^|/)\.env$"),
    re.compile(r"(^|/)\.env\.(?!example$)[^.]+$"),
    re.compile(r"^candidate_data/(?!\.gitkeep$)"),
    re.compile(r"^data/app\.db"),
    re.compile(r"^data/.*\.db(-shm|-wal|-journal)?$"),
    re.compile(r"^data/private/"),
    re.compile(r"^data/browser_assist_runtime/"),
    re.compile(r"^runtime/"),
    re.compile(r"^output/(?!\.gitkeep$)"),
    re.compile(r"storage[_-]?state\.json$"),
    re.compile(r"\.cookies?$"),
    re.compile(r"(^|/)cookies\.json$"),
]

# Regex content patterns for common secret shapes. Kept intentionally simple
# and well-known (not a proprietary secret-detection product) -- see module
# docstring's honesty caveat.
CONTENT_PATTERNS = [
    ("AWS access key ID", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("AWS secret access key (assignment)", re.compile(r"aws_secret_access_key\s*=\s*['\"]?[A-Za-z0-9/+=]{40}['\"]?", re.I)),
    ("private key block", re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("generic API key assignment", re.compile(r"\b(api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token)\b\s*[:=]\s*['\"][A-Za-z0-9\-_./+]{16,}['\"]", re.I)),
    ("password assignment (non-placeholder)", re.compile(r"(?<![\"'])\bpassword\b\s*[:=]\s*['\"](?!.*(changeme|example|placeholder|your_|<|xxxx|password123|hunter2|\$\{)).{6,}['\"]", re.I)),
    ("Slack token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9\-_.]{20,}\b")),
    ("connection string with credentials", re.compile(
        r"[a-z][a-z0-9+]*://(?!(user|username|pass|password|changeme|example|your_|xxxx|admin)\b)"
        r"[^\s'\"/:]+:(?!(pass|password|changeme|example|your_|xxxx)\b)[^\s'\"/@]{4,}@[^\s'\"/]+", re.I,
    )),
]

# Files/extensions never worth scanning as text (binary or huge) -- avoids
# false positives/crashes on binary content.
_SKIP_EXTENSIONS = {".pyc", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".docx", ".ico", ".woff", ".woff2", ".zip"}

# Lines in this scanner's own source and .env.example are expected to
# contain pattern-example text (e.g. documenting what NOT to commit) --
# never flag this file or the documented example file against themselves.
# The two test files below deliberately contain realistic-but-fake secret-
# shaped fixture strings (AWS key format, private key block, connection
# strings) to exercise CONTENT_PATTERNS/_redact_database_url's own detection
# logic -- pre-existing on this branch, not something this scanner should
# ever treat as a real finding about the repository's actual contents.
_SELF_EXEMPT = {
    "scripts/secret_scan.py", ".env.example", "docs/data-retention.md",
    "tests/test_secret_scan.py", "tests/test_config_doctor.py",
}


def _tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    return [line for line in out.stdout.splitlines() if line.strip()]


def _all_files() -> list[str]:
    tracked = set(_tracked_files())
    out = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    untracked = [line for line in out.stdout.splitlines() if line.strip()]
    return sorted(tracked | set(untracked))


def scan(paths: list[str]) -> list[str]:
    findings: list[str] = []
    for rel_path in paths:
        for pattern in FORBIDDEN_PATH_PATTERNS:
            if pattern.search(rel_path):
                findings.append(f"FORBIDDEN PATH TRACKED: {rel_path} (matches {pattern.pattern})")
                break

        if rel_path in _SELF_EXEMPT:
            continue
        full = REPO_ROOT / rel_path
        if full.suffix.lower() in _SKIP_EXTENSIONS or not full.is_file():
            continue
        try:
            text = full.read_text(errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue
        if len(text) > 2_000_000:
            continue  # not worth scanning very large generated/data files line by line
        for label, pattern in CONTENT_PATTERNS:
            m = pattern.search(text)
            if m:
                line_no = text.count("\n", 0, m.start()) + 1
                findings.append(f"POSSIBLE SECRET ({label}): {rel_path}:{line_no}")
    return findings


def main() -> int:
    scan_all = "--all" in sys.argv[1:]
    paths = _all_files() if scan_all else _tracked_files()
    findings = scan(paths)
    print(f"Secret scan: {len(paths)} file(s) checked ({'tracked + untracked' if scan_all else 'git-tracked only'}).")
    if not findings:
        print("No obvious secret patterns or forbidden tracked paths found.")
        print("(Pattern-based scan -- not a guarantee of zero secrets. See module docstring.)")
        return 0
    print(f"{len(findings)} finding(s):")
    for f in findings:
        print(f"  - {f}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
