import json

from app.registry.cli import build_parser, main


def _write_csv(tmp_path, name="companies.csv"):
    path = tmp_path / name
    path.write_text(
        "company_name,company_domain,provider,tenant_identifier,careers_url,country,source,source_url\n"
        "Acme Corp,acme.com,greenhouse,acme,https://boards.greenhouse.io/acme,US,manual_seed,https://acme.com\n",
        encoding="utf-8",
    )
    return path


def test_cli_parser_has_all_subcommands():
    parser = build_parser()
    sub_actions = [a for a in parser._subparsers._group_actions if hasattr(a, "choices")]
    commands = set(sub_actions[0].choices.keys())
    assert commands == {
        "import", "validate", "stats", "export", "doctor", "verify",
        "acquire", "batches", "resume",
    }


def test_cli_import_command(tmp_env, tmp_path, capsys):
    path = _write_csv(tmp_path)
    code = main(["import", str(path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "rows_created:     1" in out


def test_cli_validate_dry_run_writes_nothing(tmp_env, tmp_path, capsys):
    path = _write_csv(tmp_path)
    code = main(["validate", str(path)])
    assert code == 0
    from app.registry import store
    assert store.count_companies() == 0


def test_cli_validate_reports_invalid_rows_nonzero_exit(tmp_env, tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("company_name,provider\n,greenhouse\n", encoding="utf-8")
    code = main(["validate", str(path)])
    assert code == 1


def test_cli_stats_command(tmp_env, capsys):
    code = main(["stats"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Companies:" in out
    assert "Portals:" in out


def test_cli_export_command(tmp_env, tmp_path, capsys):
    _write_csv(tmp_path)
    main(["import", str(_write_csv(tmp_path))])
    out_path = tmp_path / "out.jsonl"
    code = main(["export", str(out_path)])
    assert code == 0
    assert out_path.exists()
    lines = out_path.read_text().strip().splitlines()
    assert len(lines) == 1


def test_cli_doctor_command_clean_registry(tmp_env, capsys):
    code = main(["doctor"])
    assert code == 0
    out = capsys.readouterr().out
    assert "0 serious issue" in out


def test_cli_import_then_doctor_no_issues(tmp_env, tmp_path):
    main(["import", str(_write_csv(tmp_path))])
    code = main(["doctor"])
    assert code == 0
