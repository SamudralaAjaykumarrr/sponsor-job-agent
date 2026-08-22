"""CLAUDE.md Phase 10 sections 35-36: browser-assist navigation-safety
allowlist. Pure logic, no browser/DB involved."""

from app.applications.domain_allowlist import is_allowed_domain, is_allowed_host_for_session


def test_greenhouse_domain_allowed():
    assert is_allowed_domain("greenhouse", "https://boards.greenhouse.io/acme/jobs/1")
    assert is_allowed_domain("greenhouse", "https://job-boards.greenhouse.io/acme/jobs/1")


def test_unrelated_domain_rejected():
    assert not is_allowed_domain("greenhouse", "https://evil-ads-network.example.com/track")


def test_unknown_provider_never_wildcard_allows():
    assert not is_allowed_domain("some_never_configured_provider", "https://anything.example.com")


def test_file_uri_always_allowed_test_only():
    assert is_allowed_domain("greenhouse", "file:///tmp/fixture.html")


def test_same_host_as_original_always_allowed_even_for_unknown_provider():
    """A Workday tenant subdomain (e.g. acme.wd5.myworkdayjobs.com) can't be
    enumerated by a static suffix list per-tenant, but staying on the exact
    host we were told to open is always safe."""
    original = "https://acme.wd5.myworkdayjobs.com/External/job/1"
    same_host_next_page = "https://acme.wd5.myworkdayjobs.com/External/job/1/apply"
    assert is_allowed_host_for_session("workday", original, same_host_next_page)


def test_redirect_to_unrelated_host_rejected():
    original = "https://boards.greenhouse.io/acme/jobs/1"
    redirected = "https://totally-different-and-unexpected.example.net/landing"
    assert not is_allowed_host_for_session("greenhouse", original, redirected)


def test_redirect_to_known_provider_domain_allowed():
    original = "https://job-boards.greenhouse.io/acme/jobs/1"
    redirected_apply_subdomain = "https://boards.greenhouse.io/embed/job_app?for=acme&token=1"
    assert is_allowed_host_for_session("greenhouse", original, redirected_apply_subdomain)


def test_empty_current_url_rejected():
    assert not is_allowed_host_for_session("greenhouse", "https://boards.greenhouse.io/acme/jobs/1", "")
