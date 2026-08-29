"""Sponsorship Intelligence Coverage V1: app.sponsorship.public_source_importer
-- the third (public-web LCA-aggregator snapshot) evidence source. Covers the
anti-contamination guard (test matrix item J: unrelated employer must never
inherit another company's sponsorship history) and provenance retention
(item H)."""

from app.registry.models import Company
from app.registry import store
from app.sponsorship.datasets import get_dataset
from app.sponsorship.evidence import list_evidence_for_company
from app.sponsorship.public_source_importer import import_h1bdata_snapshot

_ROW_TEMPLATE = (
    '<tr><td><a href="index.php?em={employer_q}&job=&city=&year=ALL+YEARS">{employer}</a></td>'
    '<td><a href="index.php?em={employer_q}&job={job_q}&city=&year=ALL+YEARS">{job_title}</a></td>'
    '<td><a href="details.php?id={case_id}" target="_blank">120,000</a></td>'
    '<td><a href="index.php?em={employer_q}&job=&city={city}&year=ALL+YEARS">{city}, {state}</a></td>'
    '<td class="d-sm-none">{submit_date}</td><td class="d-sm-none">{start_date}</td></tr>'
)


def _row(employer, job_title, case_id, city="SAN FRANCISCO", state="CA",
         submit_date="03/01/2025", start_date="09/01/2025"):
    return _ROW_TEMPLATE.format(
        employer=employer, employer_q=employer.replace(" ", "+"), job_title=job_title,
        job_q=job_title.replace(" ", "+"), case_id=case_id, city=city, state=state,
        submit_date=submit_date, start_date=start_date,
    )


def _page(*rows: str) -> str:
    return "<html><body><table><tbody>" + "".join(rows) + "</tbody></table></body></html>"


def _write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_snapshot_import_creates_evidence_with_occupation_title(tmp_env, tmp_path):
    html = _page(
        _row("STRIPE INC", "SOFTWARE ENGINEER", "I-200-25001-000001"),
        _row("STRIPE INC", "BACKEND API ENGINEER", "I-200-25002-000002"),
    )
    path = _write(tmp_path, "stripe.html", html)
    result = import_h1bdata_snapshot(path, "STRIPE INC")
    assert result.rows_total == 2
    assert result.rows_created == 2
    assert result.rows_rejected_employer_mismatch == 0

    cid = store.insert_company(Company(normalized_name="stripe", display_name="Stripe", primary_domain="stripe.com"))
    # Re-import now that the registry company exists so resolve_company can attach it.
    result2 = import_h1bdata_snapshot(path, "STRIPE INC", dataset_version="v2")
    assert result2.company_id == cid
    rows = list_evidence_for_company(cid)
    assert len(rows) == 2
    assert rows[0].occupation_title in ("SOFTWARE ENGINEER", "BACKEND API ENGINEER")
    assert rows[0].occupation_code == ""  # never fabricated -- this source has no SOC code
    assert rows[0].source_type == "OTHER_REPUTABLE_PUBLIC_SOURCE"
    assert rows[0].source_quality == "SECONDARY_REPUTABLE"  # never PRIMARY_GOVERNMENT for a third-party mirror
    assert rows[0].fiscal_year == 2025


def test_unrelated_employer_never_inherits_evidence(tmp_env, tmp_path):
    """Item J: a substring/OR-token search on the real site can surface an
    unrelated company (verified live: searching 'GITLAB' returns rows for
    'GITLAB FOUNDATION', a distinct nonprofit). Any row whose employer text
    doesn't exactly match the requested employer must be rejected, never
    imported under the caller's company."""
    html = _page(
        _row("GITLAB FOUNDATION", "MANAGER OF IMPACT MEASUREMENT", "I-200-25091-818142"),
    )
    path = _write(tmp_path, "gitlab.html", html)
    result = import_h1bdata_snapshot(path, "GITLAB")
    assert result.rows_total == 1
    assert result.rows_created == 0
    assert result.rows_rejected_employer_mismatch == 1
    assert result.rejected_employer_names == ["GITLAB FOUNDATION"]
    assert result.company_id is None


def test_mixed_page_only_matching_rows_imported(tmp_env, tmp_path):
    html = _page(
        _row("RAMP BUSINESS CORPORATION", "SOFTWARE ENGINEER", "I-200-25003-000003"),
        _row("COLORADO RAMPAGE", "ACCOUNTANT", "I-200-25004-000004"),
    )
    path = _write(tmp_path, "ramp.html", html)
    result = import_h1bdata_snapshot(path, "RAMP BUSINESS CORPORATION")
    assert result.rows_created == 1
    assert result.rows_rejected_employer_mismatch == 1
    assert result.rejected_employer_names == ["COLORADO RAMPAGE"]


def test_idempotent_on_case_id(tmp_env, tmp_path):
    html = _page(_row("BREX INC", "SOFTWARE ENGINEER", "I-200-25005-000005"))
    path = _write(tmp_path, "brex.html", html)
    r1 = import_h1bdata_snapshot(path, "BREX INC", dataset_version="v1")
    r2 = import_h1bdata_snapshot(path, "BREX INC", dataset_version="v1", dataset_id=r1.dataset_id)
    assert r1.rows_created == 1
    assert r2.rows_created == 0
    assert r2.rows_skipped_duplicate == 1


def test_provenance_fields_retained(tmp_env, tmp_path):
    html = _page(_row("ASANA INC", "STAFF ENGINEER", "I-200-25006-000006", submit_date="06/15/2025"))
    path = _write(tmp_path, "asana.html", html)
    result = import_h1bdata_snapshot(path, "ASANA INC")
    dataset = get_dataset(result.dataset_id)
    assert dataset["dataset_name"] == "h1bdata_info_lca_snapshot"
    assert dataset["source_url"] == "https://h1bdata.info"

    # Row-level provenance: source, employer identity, period, evidence type all present.
    from app.sponsorship.evidence import list_evidence_by_name

    rows = list_evidence_by_name("ASANA INC")
    assert len(rows) == 1
    row = rows[0]
    assert row.source == "H1BDATA_INFO_LCA_MIRROR"
    assert row.source_type == "OTHER_REPUTABLE_PUBLIC_SOURCE"
    assert row.company_name_raw == "ASANA INC"
    assert row.fiscal_year == 2025
    assert row.dataset_id == result.dataset_id
    assert "third-party" in row.notes.lower()


def test_missing_employer_raises(tmp_env, tmp_path):
    import pytest

    path = _write(tmp_path, "empty.html", _page())
    with pytest.raises(ValueError):
        import_h1bdata_snapshot(path, "")
