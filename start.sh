#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install -q -r requirements.txt

mkdir -p data output candidate_data

echo "Starting Sponsor Job Agent dashboard at http://127.0.0.1:8000"
exec uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
