from app.sponsorship.cli import main


def _write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_cli_import_uscis_and_stats(tmp_env, tmp_path, capsys):
    path = _write(tmp_path, "uscis.csv",
                  "Fiscal Year,Employer,Initial Approval,Initial Denial,Continuing Approval,Continuing Denial,NAICS Code,State,City\n"
                  "2024,CliCo,5,0,3,0,5415,CA,SF\n")
    rc = main(["import-uscis", str(path)])
    assert rc == 0
    rc = main(["stats"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "total_evidence_records" in out


def test_cli_datasets_and_doctor(tmp_env, capsys):
    rc = main(["doctor"])
    assert rc == 0
    rc = main(["datasets"])
    assert rc == 0


def test_cli_company_profile(tmp_env, tmp_path, capsys):
    from app.registry.models import Company
    from app.registry import store

    store.insert_company(Company(normalized_name="clico", display_name="CliCo", primary_domain="clico.com"))
    path = _write(tmp_path, "uscis.csv",
                  "Fiscal Year,Employer,Initial Approval,Initial Denial,Continuing Approval,Continuing Denial,NAICS Code,State,City\n"
                  "2024,CliCo,5,0,3,0,5415,CA,SF\n")
    main(["import-uscis", str(path)])
    rc = main(["company", "CliCo"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "HISTORICAL EVIDENCE" in out


def test_cli_review_queue_empty(tmp_env, capsys):
    rc = main(["review-queue"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "0 job(s) in review queue" in out


def test_cli_doctor_exits_nonzero_on_serious_issue(tmp_env):
    from app.db import db_session

    with db_session() as conn:
        conn.execute(
            "INSERT INTO employer_sponsorship_evidence (company_id, company_name_raw, source, observed_at, imported_at) "
            "VALUES (99999, 'GhostCo', 'test', '2024-01-01', '2024-01-01')"
        )
    rc = main(["doctor"])
    assert rc == 1
