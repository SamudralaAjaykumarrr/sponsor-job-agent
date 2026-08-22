#!/usr/bin/env python3
"""Generates the single authoritative provider capability matrix (CLAUDE.md
Phase 15 section 71): "produce one authoritative provider matrix. Separate:
discovery support, form/assist support, live verification, CAPTCHA/auth
restrictions, automatic submission support. No contradictory provider
capability tables across docs/UI."

This script never hand-maintains a duplicate copy of any capability data --
it only reads and merges (by provider name) the three tables that already
exist as each subsystem's own single source of truth:
  - app.providers.registry.all_capabilities()            (discovery)
  - app.applications.provider_registry.all_application_capabilities()  (form/assist automation)
  - app.applications.browser_capability_matrix.all_rows() (live-browser verification evidence)

Run it any time the truth needs to be re-checked; it is read-only and
prints to stdout (or writes to a path with --out) rather than being
committed as a stale snapshot itself.

Usage:
    python scripts/generate_provider_matrix.py [--out PATH]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def build_merged_matrix() -> list[dict]:
    from app.applications.browser_capability_matrix import all_rows as browser_rows
    from app.applications.provider_registry import all_application_capabilities
    from app.providers.registry import all_capabilities

    discovery_by_name = {c.provider_name: c for c in all_capabilities()}
    assist_by_name = {c["provider"]: c for c in all_application_capabilities()}
    browser_by_name = {r["provider"]: r for r in browser_rows()}

    provider_names = sorted(set(discovery_by_name) | set(assist_by_name) | set(browser_by_name))
    merged = []
    for name in provider_names:
        d = discovery_by_name.get(name)
        a = assist_by_name.get(name)
        b = browser_by_name.get(name)
        merged.append({
            "provider": name,
            "discovery_support_level": d.support_level.value if d else "N/A",
            "discovery_supported": d.discovery_supported if d else None,
            "form_assist_support_level": a.get("support_level") if a else "N/A",
            "form_assist_automation_policy": a.get("automation_policy") if a else "N/A",
            "auto_submit_supported": a.get("submission_supported") if a else False,
            "browser_live_verification": b.get("verification") if b else "NOT_TESTED",
            "browser_final_submit_automation": b.get("final_submit_automation") if b else "N/A",
            "requires_credentials": d.requires_credentials if d else None,
        })
    return merged


def render_text(rows: list[dict]) -> str:
    lines = ["Authoritative Provider Capability Matrix", "=" * 60,
              "(generated -- reflects live code declarations, never hand-edited)", ""]
    for r in rows:
        lines.append(f"Provider: {r['provider']}")
        lines.append(f"  Discovery support level:        {r['discovery_support_level']}")
        lines.append(f"  Form/assist support level:      {r['form_assist_support_level']}")
        lines.append(f"  Form/assist automation policy:  {r['form_assist_automation_policy']}")
        lines.append(f"  Auto-submit supported:          {r['auto_submit_supported']}")
        lines.append(f"  Browser live verification:      {r['browser_live_verification']}")
        lines.append(f"  Browser final-submit automation:{r['browser_final_submit_automation']}")
        lines.append("")
    real_auto_submit = [r["provider"] for r in rows if r["auto_submit_supported"]]
    lines.append("-" * 60)
    lines.append(f"Providers with auto-submit=True: {real_auto_submit or 'NONE'}")
    lines.append("(CLAUDE.md Phase 15 section 72: expected to be empty for every real ATS provider; "
                  "'mock_ats' is the only sanctioned fixture-only exception.)")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=str, default="", help="write to this path instead of stdout")
    parser.add_argument("--json", action="store_true", help="output JSON instead of text")
    args = parser.parse_args()

    rows = build_merged_matrix()
    if args.json:
        import json
        output = json.dumps(rows, indent=2)
    else:
        output = render_text(rows)

    if args.out:
        Path(args.out).write_text(output)
        print(f"Wrote {args.out}")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
