"""CLAUDE.md Phase 11 section 40: additional domain-allowlist hardening
tests -- relative URLs, javascript:/data: URLs, and company ATS subdomains.
Pure logic, no browser/DB involved."""

from app.applications.domain_allowlist import is_allowed_domain, is_allowed_host_for_session


def test_relative_url_never_blindly_trusted():
    """A real browser's page.url is always absolute, so this should never
    actually happen in practice -- but a defensive fail-safe (reject, not
    accidentally allow) is required if it ever does."""
    original = "https://boards.greenhouse.io/acme/jobs/1"
    assert not is_allowed_host_for_session("greenhouse", original, "/apply/123")


def test_javascript_url_rejected():
    original = "https://boards.greenhouse.io/acme/jobs/1"
    assert not is_allowed_host_for_session("greenhouse", original, "javascript:void(0)")
    assert not is_allowed_domain("greenhouse", "javascript:alert(1)")


def test_data_url_rejected():
    original = "https://boards.greenhouse.io/acme/jobs/1"
    assert not is_allowed_host_for_session("greenhouse", original, "data:text/html,<script>evil()</script>")


def test_company_ats_subdomain_allowed_via_same_host():
    """A company's own branded ATS subdomain (not enumerable by any static
    provider suffix list) is always safe as long as it's the exact host the
    session was told to open."""
    original = "https://careers.smartrecruiters.com/AcmeCorp/backend-engineer"
    same_host = "https://careers.smartrecruiters.com/AcmeCorp/backend-engineer/apply"
    assert is_allowed_host_for_session("smartrecruiters", original, same_host)


def test_ashbyhq_provider_domain_allowed():
    assert is_allowed_domain("ashby", "https://jobs.ashbyhq.com/acme/12345")


def test_workable_apply_subdomain_allowed():
    assert is_allowed_domain("workable", "https://apply.workable.com/acme/j/ABCDEF/")


def test_unexpected_third_party_host_never_allowed_even_with_apply_looking_path():
    """A path that looks apply-shaped on an unrelated host must still be
    rejected -- domain safety is host-based, never path-based."""
    original = "https://boards.greenhouse.io/acme/jobs/1"
    assert not is_allowed_host_for_session(
        "greenhouse", original, "https://phishing-lookalike.example.net/apply/greenhouse",
    )
