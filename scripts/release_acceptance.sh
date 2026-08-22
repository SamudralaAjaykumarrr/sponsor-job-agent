#!/usr/bin/env bash
# Release-candidate acceptance entry point (CLAUDE.md Phase 15 section 81).
# Thin wrapper around `python -m app.acceptance` -- see that module's
# docstring for exactly what it checks and what it deliberately does NOT do
# (no real application submission, no internet requirement, no destructive
# DB operations). PostgreSQL/browser suites run automatically if their
# optional dependencies are already importable and skip honestly otherwise.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -d ".venv" ]; then
  echo "No .venv found -- run ./start.sh once first (or: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt)." >&2
  exit 1
fi

source .venv/bin/activate
exec python -m app.acceptance
