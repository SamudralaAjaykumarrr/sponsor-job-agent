"""Real, DB-derived sponsorship-evidence coverage metrics -- every number here
is a live query against the actual jobs/evidence/registry tables, never an
estimate (same principle as app.registry.analytics). Used for the
Sponsorship Intelligence Coverage V1 before/after report.

Scope is deliberately limited to REAL discovered employers: rows with
`is_test_fixture = 1` (the mock-ATS/demo fixtures) and the legacy
"Acme Corp" manual-ingestion placeholder are excluded from every count here,
since they were never real discovered jobs and would distort coverage
percentages."""

from app.db import db_session

_FIXTURE_EXCLUSION = "is_test_fixture = 0 AND company != 'Acme Corp'"


def coverage_snapshot() -> dict:
    with db_session() as conn:
        employer_rows = conn.execute(
            f"SELECT DISTINCT company FROM jobs WHERE {_FIXTURE_EXCLUSION}"
        ).fetchall()
        employers = [r["company"] for r in employer_rows]

        jobs_by_status = {
            r["sponsorship_status"]: r["c"]
            for r in conn.execute(
                f"SELECT sponsorship_status, COUNT(*) AS c FROM jobs WHERE {_FIXTURE_EXCLUSION} "
                "GROUP BY sponsorship_status"
            ).fetchall()
        }
        jobs_total = sum(jobs_by_status.values())

        pending_reviews = conn.execute(
            "SELECT COUNT(DISTINCT source_company_name) AS c FROM employer_identity_review WHERE status = 'PENDING'"
        ).fetchone()["c"]

    from app.sponsorship.evidence import list_evidence_for_company
    from app.sponsorship.identity import resolve_company

    matched = 0
    unmatched = []
    ambiguous = []
    for name in employers:
        match = resolve_company(name)
        if match.matched_via == "ambiguous":
            ambiguous.append(name)
        # A brand name (e.g. "Stripe") resolved via registry identity will
        # rarely equal the raw legal-entity name evidence rows are stored
        # under (e.g. "STRIPE INC") -- evidence presence must be checked by
        # the resolved company_id, never by re-comparing raw name text.
        elif match.company_id is not None and list_evidence_for_company(match.company_id, limit=1):
            matched += 1
        else:
            unmatched.append(name)

    return {
        "employers_total": len(employers),
        "employers_matched_to_evidence": matched,
        "employers_unmatched": len(unmatched),
        "employers_ambiguous": len(ambiguous),
        "unmatched_employer_names": sorted(unmatched),
        "ambiguous_employer_names": sorted(ambiguous),
        "identity_reviews_pending": pending_reviews,
        "jobs_total": jobs_total,
        "jobs_confirmed_sponsor": jobs_by_status.get("CONFIRMED_SPONSOR", 0),
        "jobs_likely_sponsor": jobs_by_status.get("LIKELY_SPONSOR", 0),
        "jobs_unknown": jobs_by_status.get("UNKNOWN", 0),
        "jobs_no_sponsorship": jobs_by_status.get("NO_SPONSORSHIP", 0),
    }
