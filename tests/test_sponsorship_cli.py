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


def test_cli_seed_identities_and_aliases_and_coverage(tmp_env, tmp_path, capsys, monkeypatch):
    import json

    from app import config

    identity_path = tmp_path / "identities.json"
    identity_path.write_text(json.dumps({
        "companies": [{"normalized_name": "cliseedco", "display_name": "CliSeedCo", "primary_domain": "cliseedco.com"}]
    }), encoding="utf-8")
    monkeypatch.setattr(config, "EMPLOYER_IDENTITY_SEED_PATH", identity_path)
    rc = main(["seed-identities"])
    assert rc == 0
    assert "companies created: 1" in capsys.readouterr().out

    alias_path = tmp_path / "aliases.json"
    alias_path.write_text(json.dumps({
        "aliases": [{"registry_normalized_name": "cliseedco", "alias": "CliSeedCo Legal Name Inc", "alias_type": "LEGAL_NAME"}]
    }), encoding="utf-8")
    monkeypatch.setattr(config, "EMPLOYER_ALIAS_SEED_PATH", alias_path)
    rc = main(["seed-aliases"])
    assert rc == 0
    assert "aliases applied: 1" in capsys.readouterr().out

    rc = main(["coverage"])
    assert rc == 0
    assert "employers_total" in capsys.readouterr().out


def test_cli_import_public_source_and_refresh_jobs(tmp_env, tmp_path, capsys):
    from app.registry.models import Company
    from app.registry import store

    store.insert_company(Company(normalized_name="publicco", display_name="PublicCo", primary_domain="publicco.com"))
    html = (
        '<html><body><table><tbody>'
        '<tr><td><a href="index.php?em=PUBLICCO+INC">PUBLICCO INC</a></td>'
        '<td><a href="index.php?em=PUBLICCO+INC&job=SOFTWARE+ENGINEER">SOFTWARE ENGINEER</a></td>'
        '<td><a href="details.php?id=I-200-25010-000010">100,000</a></td>'
        '<td><a href="index.php?em=PUBLICCO+INC&city=SF">SF, CA</a></td>'
        '<td class="d-sm-none">01/01/2025</td><td class="d-sm-none">06/01/2025</td></tr>'
        '</tbody></table></body></html>'
    )
    path = _write(tmp_path, "publicco.html", html)
    rc = main(["import-public-source", str(path), "--employer", "PUBLICCO INC"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "rows_created" in out

    from app.jobs_repo import insert_job
    from app.models import ApplicationState, Job, SponsorshipStatus

    job_id = insert_job(Job(
        title="Backend Software Engineer", company="PublicCo", company_identifier="publicco",
        location="Remote", description="Join our backend team building scalable Python APIs.",
        provider="manual", url="https://example.com/publicco",
        sponsorship_status=SponsorshipStatus.UNKNOWN, application_state=ApplicationState.ANALYZED,
    ))
    rc = main(["refresh-jobs", "--job-ids", str(job_id)])
    assert rc == 0
    assert f"job={job_id}" in capsys.readouterr().out


def test_cli_doctor_exits_nonzero_on_serious_issue(tmp_env):
    from app.db import db_session

    with db_session() as conn:
        conn.execute(
            "INSERT INTO employer_sponsorship_evidence (company_id, company_name_raw, source, observed_at, imported_at) "
            "VALUES (99999, 'GhostCo', 'test', '2024-01-01', '2024-01-01')"
        )
    rc = main(["doctor"])
    assert rc == 1
