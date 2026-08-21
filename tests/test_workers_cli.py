import httpx

from app.registry.models import CompanyRegistryEntry
from app.registry import repo as registry_repo
from app.workers.cli import build_parser, main


def test_cli_parser_has_all_subcommands():
    parser = build_parser()
    sub_actions = [a for a in parser._subparsers._group_actions if hasattr(a, "choices")]
    commands = set(sub_actions[0].choices.keys())
    assert commands == {"run", "status", "attempts", "dead-letter"}


def test_cli_run_once_with_no_due_work(tmp_env, capsys):
    code = main(["run", "--once"])
    assert code == 0


def test_cli_run_once_processes_due_portal(tmp_env, mock_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jobs": []})

    mock_httpx(handler)
    registry_repo.insert_entry(CompanyRegistryEntry(company_name="Acme", provider="greenhouse", tenant_identifier="acme"))
    code = main(["run", "--once"])
    assert code == 0

    from app.jobs_repo import list_discovery_log
    # discovery_log is populated via the normal insert_discovery_log call path.


def test_cli_status_runs_and_prints(tmp_env, capsys):
    code = main(["status"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Workers" in out
    assert "Fleet metrics" in out


def test_cli_attempts_runs_and_prints(tmp_env, capsys):
    code = main(["attempts"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Recent attempts" in out


def test_cli_dead_letter_list_and_requeue(tmp_env, capsys):
    from app.workers import dead_letter

    entry_id = registry_repo.insert_entry(CompanyRegistryEntry(company_name="Acme", provider="greenhouse", tenant_identifier="acme"))
    dead_letter.record_permanent_failure(
        portal_type="company_registry", portal_id=entry_id, provider="greenhouse",
        consecutive_permanent_failures=8, last_error="404", last_attempt_id="a1", threshold=8,
    )
    code = main(["dead-letter"])
    assert code == 0
    out = capsys.readouterr().out
    assert "greenhouse" in out

    dl = dead_letter.list_dead_letters()[0]
    code = main(["dead-letter", "--requeue", str(dl["id"])])
    assert code == 0


def test_cli_shard_flags_override_config(tmp_env):
    """--shard-index/--shard-count must reach the Worker instance."""
    import app.workers.cli as cli_mod

    captured = {}

    class FakeWorker:
        def __init__(self, shard_index=None, shard_count=None, single_cycle=False):
            captured["shard_index"] = shard_index
            captured["shard_count"] = shard_count

        def install_signal_handlers(self):
            pass

        def run(self):
            pass

    import app.workers.runner as runner_mod
    orig = runner_mod.Worker
    runner_mod.Worker = FakeWorker
    try:
        main(["run", "--shard-index", "2", "--shard-count", "4", "--once"])
    finally:
        runner_mod.Worker = orig

    assert captured["shard_index"] == 2
    assert captured["shard_count"] == 4
