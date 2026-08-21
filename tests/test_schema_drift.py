import httpx

from app.workers import schema_check


def _resp(json_body) -> httpx.Response:
    return httpx.Response(200, json=json_body, request=httpx.Request("GET", "https://example.com"))


def test_greenhouse_healthy_empty_board_is_not_drift():
    result = schema_check.check_shape("greenhouse", _resp({"jobs": []}))
    assert result.ok is True


def test_greenhouse_missing_jobs_key_is_drift():
    result = schema_check.check_shape("greenhouse", _resp({"unexpected": "shape"}))
    assert result.ok is False
    assert "missing" in result.detail


def test_greenhouse_wrong_type_for_jobs_is_drift():
    result = schema_check.check_shape("greenhouse", _resp({"jobs": "not-a-list"}))
    assert result.ok is False


def test_lever_healthy_empty_board_is_not_drift():
    result = schema_check.check_shape("lever", _resp([]))
    assert result.ok is True


def test_lever_non_list_top_level_is_drift():
    result = schema_check.check_shape("lever", _resp({"postings": []}))
    assert result.ok is False


def test_ashby_missing_jobs_key_is_drift():
    result = schema_check.check_shape("ashby", _resp({"organizationName": "Acme"}))
    assert result.ok is False


def test_ashby_healthy_empty_board():
    result = schema_check.check_shape("ashby", _resp({"organizationName": "Acme", "jobs": []}))
    assert result.ok is True


def test_workable_shape():
    assert schema_check.check_shape("workable", _resp({"jobs": []})).ok is True
    assert schema_check.check_shape("workable", _resp({"nope": []})).ok is False


def test_smartrecruiters_shape():
    assert schema_check.check_shape("smartrecruiters", _resp({"content": []})).ok is True
    assert schema_check.check_shape("smartrecruiters", _resp({})).ok is False


def test_bamboohr_shape():
    assert schema_check.check_shape("bamboohr", _resp({"result": []})).ok is True
    assert schema_check.check_shape("bamboohr", _resp({"result": "bad"})).ok is False


def test_recruitee_shape():
    assert schema_check.check_shape("recruitee", _resp({"offers": []})).ok is True
    assert schema_check.check_shape("recruitee", _resp({})).ok is False


def test_breezy_shape():
    assert schema_check.check_shape("breezy", _resp([])).ok is True
    assert schema_check.check_shape("breezy", _resp({"jobs": []})).ok is False


def test_comeet_accepts_either_list_or_positions_key():
    assert schema_check.check_shape("comeet", _resp([])).ok is True
    assert schema_check.check_shape("comeet", _resp({"positions": []})).ok is True
    assert schema_check.check_shape("comeet", _resp({"other": []})).ok is False


def test_workday_shape():
    assert schema_check.check_shape("workday", _resp({"jobPostings": [], "total": 0})).ok is True
    assert schema_check.check_shape("workday", _resp({"total": 0})).ok is False


def test_invalid_json_body_is_drift():
    bad_resp = httpx.Response(200, content=b"not json at all", request=httpx.Request("GET", "https://example.com"))
    result = schema_check.check_shape("greenhouse", bad_resp)
    assert result.ok is False


def test_unknown_provider_has_no_check_and_is_treated_as_ok():
    assert schema_check.has_shape_check("teamtailor") is False
    result = schema_check.check_shape("teamtailor", _resp({"anything": True}))
    assert result.ok is True


def test_has_shape_check_covers_every_provider_with_a_probe():
    from app.registry import probe as probe_mod

    for provider in probe_mod._PROBES:
        assert schema_check.has_shape_check(provider), f"{provider} has a probe but no shape check"
