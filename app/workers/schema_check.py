"""Distinguishes a provider that structurally changed its response shape
("schema drift") from a provider board that legitimately has zero current
jobs ("empty board") -- CLAUDE.md Phase 5 sections 26/27 require these be
told apart, since they mean very different things operationally (drift is a
connector bug needing attention; an empty board is normal and healthy).

Only covers providers with a raw structural probe (app.registry.probe) --
which, by construction, is every provider a portal can actually reach
ACTIVE/pollable status for (app.registry.verification.verify_portal refuses
to VERIFY a portal for a provider with no probe). So this has full coverage
of everything the worker fleet actually polls, with no silent gaps.

Each check only asserts that the expected top-level container is present
AND is the expected JSON type (a list, or a dict with a list under a known
key) -- it does NOT validate every field, since that would make this
brittle to harmless upstream additions. A truly malformed/unexpected shape
(missing key, wrong type, unparseable body) is schema drift; a present,
correctly-typed, empty container is a healthy empty board."""

from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class ShapeCheckResult:
    ok: bool
    detail: str


def _expect_list_at(response: httpx.Response, *, key: str | None) -> ShapeCheckResult:
    try:
        data = response.json()
    except ValueError as exc:
        return ShapeCheckResult(False, f"response body is not valid JSON: {exc}")

    if key is None:
        if not isinstance(data, list):
            return ShapeCheckResult(False, f"expected a top-level JSON list, got {type(data).__name__}")
        return ShapeCheckResult(True, f"top-level list present ({len(data)} item(s))")

    if not isinstance(data, dict):
        return ShapeCheckResult(False, f"expected a top-level JSON object, got {type(data).__name__}")
    if key not in data:
        return ShapeCheckResult(False, f"expected field '{key}' missing from response")
    if not isinstance(data[key], list):
        return ShapeCheckResult(False, f"expected field '{key}' to be a list, got {type(data[key]).__name__}")
    return ShapeCheckResult(True, f"'{key}' list present ({len(data[key])} item(s))")


def _check_greenhouse(resp: httpx.Response) -> ShapeCheckResult:
    return _expect_list_at(resp, key="jobs")


def _check_lever(resp: httpx.Response) -> ShapeCheckResult:
    return _expect_list_at(resp, key=None)


def _check_ashby(resp: httpx.Response) -> ShapeCheckResult:
    return _expect_list_at(resp, key="jobs")


def _check_workable(resp: httpx.Response) -> ShapeCheckResult:
    return _expect_list_at(resp, key="jobs")


def _check_smartrecruiters(resp: httpx.Response) -> ShapeCheckResult:
    return _expect_list_at(resp, key="content")


def _check_bamboohr(resp: httpx.Response) -> ShapeCheckResult:
    return _expect_list_at(resp, key="result")


def _check_recruitee(resp: httpx.Response) -> ShapeCheckResult:
    return _expect_list_at(resp, key="offers")


def _check_breezy(resp: httpx.Response) -> ShapeCheckResult:
    return _expect_list_at(resp, key=None)


def _check_comeet(resp: httpx.Response) -> ShapeCheckResult:
    # CometProvider accepts either a bare top-level list OR {"positions": [...]}
    # -- mirror that same tolerance here rather than flagging the shape the
    # real parser already treats as valid.
    try:
        data = resp.json()
    except ValueError as exc:
        return ShapeCheckResult(False, f"response body is not valid JSON: {exc}")
    if isinstance(data, list):
        return ShapeCheckResult(True, f"top-level list present ({len(data)} item(s))")
    if isinstance(data, dict) and isinstance(data.get("positions"), list):
        return ShapeCheckResult(True, f"'positions' list present ({len(data['positions'])} item(s))")
    return ShapeCheckResult(False, "expected a top-level list or a 'positions' list field")


def _check_workday(resp: httpx.Response) -> ShapeCheckResult:
    return _expect_list_at(resp, key="jobPostings")


_CHECKS = {
    "greenhouse": _check_greenhouse,
    "lever": _check_lever,
    "ashby": _check_ashby,
    "workable": _check_workable,
    "smartrecruiters": _check_smartrecruiters,
    "bamboohr": _check_bamboohr,
    "recruitee": _check_recruitee,
    "breezy": _check_breezy,
    "comeet": _check_comeet,
    "workday": _check_workday,
}


def has_shape_check(provider: str) -> bool:
    return provider.strip().lower() in _CHECKS


def check_shape(provider: str, response: httpx.Response) -> ShapeCheckResult:
    fn = _CHECKS.get(provider.strip().lower())
    if fn is None:
        return ShapeCheckResult(True, "no shape check implemented for this provider -- treated as OK")
    return fn(response)
