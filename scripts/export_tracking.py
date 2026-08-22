#!/usr/bin/env python3
"""Safe job/application tracking export (CLAUDE.md Phase 15 section 75).
CSV or JSON of the jobs table's tracking-relevant fields only -- never the
candidate's private profile (candidate_data/profile.json is never read by
this script at all). Read-only against the real database.

Usage:
    python scripts/export_tracking.py --format csv --out output/tracking_export.csv
    python scripts/export_tracking.py --format json --out output/tracking_export.json
"""

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Deliberately allow-listed, not "every jobs.* column" -- keeps this export
# stable and reviewable even as unrelated columns are added to the jobs
# table in future maintenance, and keeps it obviously free of anything
# resembling candidate PII (which the jobs table doesn't store anyway --
# see docs/data-retention.md).
_EXPORT_FIELDS = [
    "id", "title", "company", "location", "work_arrangement", "employment_type",
    "sponsorship_status", "sponsorship_conflict", "freshness_tier", "priority_tier",
    "priority_score", "technical_match_score", "application_state", "mode",
    "provider", "source", "url", "published_at", "first_seen_at",
    "created_at", "updated_at",
]


def export_rows() -> list[dict]:
    from app.jobs_repo import list_jobs

    jobs = list_jobs()
    rows = []
    for job in jobs:
        data = job.model_dump()
        rows.append({k: data.get(k) for k in _EXPORT_FIELDS})
    return rows


def write_csv(rows: list[dict], out_path: Path) -> None:
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_EXPORT_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_json(rows: list[dict], out_path: Path) -> None:
    out_path.write_text(json.dumps(rows, indent=2, default=str))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--format", choices=["csv", "json"], default="csv")
    parser.add_argument("--out", type=str, required=True, help="output file path")
    args = parser.parse_args()

    rows = export_rows()
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if args.format == "csv":
        write_csv(rows, out_path)
    else:
        write_json(rows, out_path)
    print(f"Exported {len(rows)} job/application tracking row(s) to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
