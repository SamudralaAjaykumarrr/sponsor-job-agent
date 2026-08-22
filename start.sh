#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install -q -r requirements.txt

mkdir -p data output candidate_data

# Phase 6 startup summary (CLAUDE.md section 39) -- never prints secrets
# (DATABASE_URL's password, if any, is never echoed).
DB_BACKEND="SQLite (data/app.db)"
if [ -n "${DATABASE_URL:-}" ] && [[ "${DATABASE_URL}" == postgres* ]]; then
  DB_BACKEND="PostgreSQL (shared)"
fi
QUEUE_BACKEND="SQLite (WAL + busy_timeout)"
if [ "$DB_BACKEND" = "PostgreSQL (shared)" ]; then
  QUEUE_BACKEND="PostgreSQL (SELECT ... FOR UPDATE SKIP LOCKED)"
fi
AGENT_ENABLED_DISPLAY="${AGENT_ENABLED:-false}"
APPLICATION_EXECUTOR_DISPLAY="${APPLICATION_EXECUTOR_ENABLED:-false}"
AUTO_PREPARE_DISPLAY="${APPLICATION_AUTO_PREPARE_ENABLED:-false}"
BROWSER_ASSIST_DISPLAY="${BROWSER_ASSIST_ENABLED:-false}"
BROWSER_MODE_DISPLAY="VISIBLE"
if [ "${BROWSER_HEADLESS:-${BROWSER_ASSIST_HEADLESS:-false}}" = "true" ]; then
  BROWSER_MODE_DISPLAY="HEADLESS"
fi
AUTO_SUBMIT_DISPLAY="${AUTO_SUBMIT_ENABLED:-false}"
ATS_CANARY_DISPLAY="${REAL_ATS_CANARY_ENABLED:-false}"
JOB_IDENTITY_GATE_DISPLAY="${APPLICATION_IDENTITY_REQUIRED:-true}"

echo "============================================================"
echo " Sponsor Job Agent"
echo "   Database backend:   ${DB_BACKEND}"
echo "   Queue backend:      ${QUEUE_BACKEND}"
echo "   Agent enabled:      ${AGENT_ENABLED_DISPLAY}"
echo "   Application exec:   ${APPLICATION_EXECUTOR_DISPLAY}"
echo "   Auto prepare:       ${AUTO_PREPARE_DISPLAY}"
echo "   Browser assist:     ${BROWSER_ASSIST_DISPLAY}"
echo "   Browser mode:       ${BROWSER_MODE_DISPLAY}"
echo "   Auto submit:        ${AUTO_SUBMIT_DISPLAY}"
echo "   ATS canary:         ${ATS_CANARY_DISPLAY}"
echo "   Job identity gate:  ${JOB_IDENTITY_GATE_DISPLAY}"
echo "   Worker mode:         run distributed workers separately via"
echo "                        'python -m app.workers.cli run' (see docs/fleet-operations.md)"
echo "   Dashboard URL:       http://127.0.0.1:8000"
echo "============================================================"

exec uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
