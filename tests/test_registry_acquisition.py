import csv

import httpx

from app.registry import acquisition, store


def _write_csv(tmp_path, rows):
    path = tmp_path / "seed.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["company_name", "provider", "tenant_identifier", "country", "source"])
        for row in rows:
            w.writerow(row)
    return path


def _mock_all_ok(mock_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jobs": []})

    mock_httpx(handler)


def test_acquisition_batch_end_to_end(tmp_env, tmp_path, mock_httpx):
    _mock_all_ok(mock_httpx)
    path = _write_csv(tmp_path, [
        ("Acme Corp", "greenhouse", "acme", "US", "test_seed"),
        ("Widget Co", "greenhouse", "widgetco", "US", "test_seed"),
    ])
    result = acquisition.run_acquisition_batch(path, source_name="test_seed")

    assert result.status == "COMPLETED"
    assert result.records_processed == 2
    assert result.companies_created == 2
    assert result.portal_candidates == 2
    assert result.verified == 2
    assert result.active == 2
    assert result.quarantined == 0
    assert result.failed == 0

    portals = store.list_portals()
    assert len(portals) == 2
    assert all(p.verification_status.value == "ACTIVE" for p in portals)


def test_acquisition_isolates_per_record_failure(tmp_env, tmp_path, mock_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        if "goodco" in str(request.url):
            return httpx.Response(200, json={"jobs": []})
        return httpx.Response(404, text="not found")

    mock_httpx(handler)
    path = _write_csv(tmp_path, [
        ("Good Co", "greenhouse", "goodco", "US", "test_seed"),
        ("Bad Co", "greenhouse", "doesnotexist", "US", "test_seed"),
    ])
    result = acquisition.run_acquisition_batch(path, source_name="test_seed")

    assert result.status == "COMPLETED"  # one bad record never aborts the batch
    assert result.portal_candidates == 2
    assert result.active == 1
    assert result.failed == 1


def test_acquisition_records_batch_progress_and_is_listable(tmp_env, tmp_path, mock_httpx):
    _mock_all_ok(mock_httpx)
    path = _write_csv(tmp_path, [("Acme", "greenhouse", "acme", "US", "s")])
    acquisition.run_acquisition_batch(path, source_name="s")
    batches = acquisition.list_batches()
    assert len(batches) == 1
    assert batches[0]["status"] == "COMPLETED"


def test_acquisition_resume_after_crash_is_idempotent_no_duplicates(tmp_env, tmp_path, mock_httpx):
    _mock_all_ok(mock_httpx)
    path = _write_csv(tmp_path, [(f"Company{i}", "greenhouse", f"tenant{i}", "US", "s") for i in range(5)])

    orig_read = acquisition.read_candidates
    seen = {"n": 0}

    def flaky_read(p):
        for c in orig_read(p):
            seen["n"] += 1
            if seen["n"] == 3:
                raise RuntimeError("simulated crash")
            yield c

    acquisition.read_candidates = flaky_read
    try:
        try:
            acquisition.run_acquisition_batch(path, source_name="s", checkpoint_every=1)
            assert False, "expected simulated crash"
        except RuntimeError:
            pass
    finally:
        acquisition.read_candidates = orig_read

    batches = acquisition.list_batches()
    batch = batches[0]
    assert batch["status"] == "FAILED"
    assert batch["resume_cursor"] == 2

    result = acquisition.run_acquisition_batch(path, resume_batch_id=batch["id"], checkpoint_every=1)
    assert result.status == "COMPLETED"
    assert result.records_processed == 5
    assert result.portal_candidates == 5

    portals = store.list_portals()
    assert len(portals) == 5  # no duplicates from resume


def test_resuming_completed_batch_is_a_noop(tmp_env, tmp_path, mock_httpx):
    _mock_all_ok(mock_httpx)
    path = _write_csv(tmp_path, [("Acme", "greenhouse", "acme", "US", "s")])
    first = acquisition.run_acquisition_batch(path, source_name="s")
    second = acquisition.run_acquisition_batch(path, resume_batch_id=first.batch_id)
    assert second.status == "COMPLETED"
    assert second.records_processed == first.records_processed
    assert len(store.list_portals()) == 1


def test_resuming_unknown_batch_id_raises(tmp_env, tmp_path):
    try:
        acquisition.run_acquisition_batch(tmp_path / "x.csv", resume_batch_id=999999)
        assert False
    except ValueError:
        pass


def test_reimporting_same_source_does_not_duplicate_companies(tmp_env, tmp_path, mock_httpx):
    _mock_all_ok(mock_httpx)
    path = _write_csv(tmp_path, [("Acme", "greenhouse", "acme", "US", "s")])
    acquisition.run_acquisition_batch(path, source_name="s")
    acquisition.run_acquisition_batch(path, source_name="s")  # separate, brand-new batch, same file
    assert store.count_companies() == 1
    assert len(store.list_portals()) == 1
