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
ONE_PAGE_RESUME_DISPLAY="${ONE_PAGE_RESUME_REQUIRED:-true}"
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
RESUME_OPTIMIZATION_DISPLAY="${RESUME_OPTIMIZATION_ENABLED:-false}"

# CLAUDE.md Phase 15 section 15: schema version + registry portal count.
# Best-effort -- a brand-new/unreadable DB must never block startup, so any
# failure here falls back to an honest "unknown" rather than aborting.
# "registry contains N portals" wording (never "monitoring N portals") per
# docs/scaling-claims.md -- storing a row is not the same as monitoring it.
read -r SCHEMA_VERSION_DISPLAY REGISTRY_COUNT_DISPLAY <<< "$(python -c "
from app.db import init_db, db_session
from app import migrations
try:
    init_db()
    with db_session() as conn:
        version = migrations.current_db_version(conn)
        count = conn.execute('SELECT COUNT(*) AS c FROM registry_portals').fetchone()['c']
    print(version, count)
except Exception:
    print('unknown', 'unknown')
" 2>/dev/null || echo "unknown unknown")"

# CLAUDE.md production-v2 section 94: the one-click agent's own persisted
# desired/actual state (not the legacy flag above), current interval, and
# real-provider submission count -- best-effort, same "never block startup"
# fallback as the schema-version query above.
read -r AGENT_DESIRED_DISPLAY AGENT_ACTUAL_DISPLAY AGENT_SUBMIT_PROVIDERS_DISPLAY <<< "$(python -c "
from app.db import init_db
from app.agent.run_state import get_run_state
try:
    init_db()
    run = get_run_state()
    from app.applications.provider_registry import all_application_capabilities
    caps = all_application_capabilities()
    submit_count = sum(1 for c in caps if c.get('submission_supported') and c.get('provider') != 'mock_ats')
    print(run['desired_state'], run['actual_state'], submit_count)
except Exception:
    print('unknown', 'unknown', 'unknown')
" 2>/dev/null || echo "unknown unknown unknown")"
AGENT_INTERVAL_DISPLAY="${AGENT_INTERVAL_MINUTES:-${DISCOVERY_INTERVAL_MINUTES:-15}}"

echo "============================================================"
echo " Sponsor Job Agent"
echo "   Database backend:   ${DB_BACKEND}"
echo "   Schema version:     ${SCHEMA_VERSION_DISPLAY}"
echo "   Queue backend:      ${QUEUE_BACKEND}"
echo "   Agent (legacy discovery-only flag): ${AGENT_ENABLED_DISPLAY}"
echo "   Registry:            contains ${REGISTRY_COUNT_DISPLAY} portal(s) (see docs/scaling-claims.md)"
echo "   One-page resumes:   ${ONE_PAGE_RESUME_DISPLAY} (see docs/one-page-resume-contract.md)"
echo "   Resume optimization: ${RESUME_OPTIMIZATION_DISPLAY} (manual Generate/Regenerate always available regardless)"
echo "   Application exec:   ${APPLICATION_EXECUTOR_DISPLAY}"
echo "   Auto prepare:       ${AUTO_PREPARE_DISPLAY}"
echo "   Browser assist:     ${BROWSER_ASSIST_DISPLAY}"
echo "   Browser mode:       ${BROWSER_MODE_DISPLAY}"
echo "   Auto submit:        ${AUTO_SUBMIT_DISPLAY}"
echo "   ATS canary:         ${ATS_CANARY_DISPLAY}"
echo "   Job identity gate:  ${JOB_IDENTITY_GATE_DISPLAY}"
echo "   Agent state:        desired=${AGENT_DESIRED_DISPLAY} actual=${AGENT_ACTUAL_DISPLAY} (persisted -- survives restart)"
echo "   Agent interval:     ${AGENT_INTERVAL_DISPLAY}m"
echo "   Real ATS providers with auto-submit: ${AGENT_SUBMIT_PROVIDERS_DISPLAY} (mock_ats/test-only excluded)"
echo "   Worker mode:         run distributed workers separately via"
echo "                        'python -m app.workers.cli run' (see docs/fleet-operations.md)"
echo "   One-click agent:      click START AGENT on the dashboard, or POST /agent/start"
echo "                        (see docs/one-click-agent.md). The flags above are the"
echo "                        standalone/opt-in settings; the agent overrides"
echo "                        Application exec/Auto prepare only while it is RUNNING."
echo "   Dashboard URL:       http://127.0.0.1:8000"
echo "============================================================"

exec uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
