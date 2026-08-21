import json

from app.registry import store
from app.registry.importers import import_candidates, import_file, RegistryCandidate
from app.registry.models import PortalStatus


def _write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


CSV_HEADER = "company_name,company_domain,provider,tenant_identifier,careers_url,country,source,source_url\n"


def test_csv_import_creates_companies_and_portals(tmp_env, tmp_path):
    path = _write(tmp_path, "companies.csv", CSV_HEADER +
                  "Acme Corp,acme.com,greenhouse,acme,https://boards.greenhouse.io/acme,US,manual_seed,https://acme.com/careers\n"
                  "Beta Inc,beta.com,lever,beta,https://jobs.lever.co/beta,US,manual_seed,https://beta.com/careers\n")
    summary = import_file(path)
    assert summary.rows_total == 2
    assert summary.rows_created == 2
    assert summary.companies_created == 2
    assert store.count_companies() == 2
    assert store.count_portals() == 2


def test_csv_import_idempotent_on_second_pass(tmp_env, tmp_path):
    path = _write(tmp_path, "companies.csv", CSV_HEADER +
                  "Acme Corp,acme.com,greenhouse,acme,https://boards.greenhouse.io/acme,US,manual_seed,\n")
    s1 = import_file(path)
    s2 = import_file(path)
    assert s1.rows_created == 1
    assert s2.rows_created == 0
    assert s2.rows_updated == 1
    assert store.count_companies() == 1
    assert store.count_portals() == 1


def test_100_row_csv_import_no_duplicates_on_reimport(tmp_env, tmp_path):
    rows = [CSV_HEADER.strip()]
    for i in range(100):
        rows.append(f"Company {i},company{i}.com,greenhouse,company{i},https://boards.greenhouse.io/company{i},US,bulk_seed,")
    path = _write(tmp_path, "hundred.csv", "\n".join(rows) + "\n")

    s1 = import_file(path)
    assert s1.rows_created == 100
    assert s1.rows_invalid == 0
    assert store.count_companies() == 100
    assert store.count_portals() == 100

    s2 = import_file(path)
    assert s2.rows_created == 0
    assert s2.rows_updated == 100
    assert store.count_companies() == 100
    assert store.count_portals() == 100


def test_json_import(tmp_env, tmp_path):
    data = [
        {"company_name": "Acme Corp", "company_domain": "acme.com", "provider": "greenhouse", "tenant_identifier": "acme"},
    ]
    path = tmp_path / "companies.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    summary = import_file(path)
    assert summary.rows_created == 1


def test_jsonl_import(tmp_env, tmp_path):
    lines = [
        json.dumps({"company_name": "Acme Corp", "provider": "greenhouse", "tenant_identifier": "acme"}),
        json.dumps({"company_name": "Beta Inc", "provider": "lever", "tenant_identifier": "beta"}),
    ]
    path = tmp_path / "companies.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary = import_file(path)
    assert summary.rows_created == 2


def test_dry_run_writes_nothing(tmp_env, tmp_path):
    path = _write(tmp_path, "companies.csv", CSV_HEADER +
                  "Acme Corp,acme.com,greenhouse,acme,https://boards.greenhouse.io/acme,US,manual_seed,\n")
    summary = import_file(path, dry_run=True)
    assert summary.rows_created == 1
    assert store.count_companies() == 0
    assert store.count_portals() == 0


def test_invalid_row_reported_not_silently_dropped(tmp_env, tmp_path):
    path = _write(tmp_path, "companies.csv", CSV_HEADER + ",,,,,,,\n")
    summary = import_file(path)
    assert summary.rows_invalid == 1
    assert "missing company_name" in summary.errors[0]


def test_invalid_careers_url_reported(tmp_env, tmp_path):
    path = _write(tmp_path, "companies.csv", CSV_HEADER + "Acme Corp,,,,not-a-url,,,\n")
    summary = import_file(path)
    assert summary.rows_invalid == 1


def test_company_only_row_creates_no_portal(tmp_env, tmp_path):
    path = _write(tmp_path, "companies.csv", CSV_HEADER + "Acme Corp,acme.com,,,,,manual_seed,\n")
    summary = import_file(path)
    assert summary.rows_skipped == 1
    assert store.count_companies() == 1
    assert store.count_portals() == 0


def test_provenance_recorded_on_import(tmp_env, tmp_path):
    path = _write(tmp_path, "companies.csv", CSV_HEADER +
                  "Acme Corp,acme.com,greenhouse,acme,https://boards.greenhouse.io/acme,US,manual_seed,https://acme.com\n")
    import_file(path, source_name="test-source")
    portals = store.list_portals()
    assert len(portals) == 1
    provenance = store.list_provenance_for_portal(portals[0].id)
    assert len(provenance) == 1
    assert provenance[0].source_type == "bulk_import"
    assert provenance[0].source_name == "manual_seed"  # explicit `source` column wins over source_name arg


def test_scenario_c_workday_like_url_without_reliable_tenant_stays_candidate(tmp_env):
    """CLAUDE.md Phase 4 scenario C: a candidate URL that merely LOOKS like it
    could be an ATS but has no deterministically-extractable tenant must never
    be marked VERIFIED and must never get a fabricated tenant_identifier."""
    candidates = [RegistryCandidate(company_name="Weird Co", careers_url="https://careers.weirdco.example/jobs")]
    summary = import_candidates(candidates, source_name="manual")
    assert summary.rows_created == 1
    portal = store.list_portals()[0]
    assert portal.tenant_identifier == ""
    assert portal.verification_status == PortalStatus.DISCOVERED


def test_bulk_import_never_invents_fields():
    """No provider/tenant is derived beyond what detect_provider() can
    deterministically parse from the given URL -- this is a documentation
    test asserting the importer doesn't call any network/guessing code path."""
    import inspect

    from app.registry import importers
    source = inspect.getsource(importers)
    assert "requests.get" not in source
    assert "httpx.get" not in source
