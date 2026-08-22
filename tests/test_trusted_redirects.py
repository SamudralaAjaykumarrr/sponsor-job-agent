"""CLAUDE.md Phase 12 sections 8-9, 63: deterministic, browser-free tests for
app.applications.trusted_redirects. No Playwright import anywhere in this
file -- must always run under plain `pytest`."""

from app.applications.trusted_redirects import (
    RedirectTrust,
    UrlProvenance,
    classify_redirect_trust,
    resolve_application_url,
)


def test_relative_href_is_same_host():
    decision = classify_redirect_trust("careers.acme.com", "/apply/123")
    assert decision.trust == RedirectTrust.SAME_HOST


def test_same_host_absolute_href_is_same_host():
    decision = classify_redirect_trust("careers.acme.com", "https://careers.acme.com/apply/123")
    assert decision.trust == RedirectTrust.SAME_HOST


def test_company_career_page_to_greenhouse_is_trusted():
    decision = classify_redirect_trust("careers.acme.com", "https://boards.greenhouse.io/acme/jobs/123")
    assert decision.trust == RedirectTrust.TRUSTED_ATS_REDIRECT
    assert decision.matched_provider == "greenhouse"


def test_company_career_page_to_lever_is_trusted():
    decision = classify_redirect_trust("careers.acme.com", "https://jobs.lever.co/acme/abc-123")
    assert decision.trust == RedirectTrust.TRUSTED_ATS_REDIRECT
    assert decision.matched_provider == "lever"


def test_unrelated_host_is_untrusted():
    decision = classify_redirect_trust("careers.acme.com", "https://ads.example-tracker.com/click?x=1")
    assert decision.trust == RedirectTrust.UNTRUSTED


def test_javascript_scheme_is_unsafe():
    decision = classify_redirect_trust("careers.acme.com", "javascript:void(0)")
    assert decision.trust == RedirectTrust.UNSAFE_SCHEME


def test_data_scheme_is_unsafe():
    decision = classify_redirect_trust("careers.acme.com", "data:text/html,<script>evil()</script>")
    assert decision.trust == RedirectTrust.UNSAFE_SCHEME


def test_malformed_url_is_untrusted_not_a_crash():
    decision = classify_redirect_trust("careers.acme.com", "http://[::1")
    assert decision.trust in (RedirectTrust.UNTRUSTED, RedirectTrust.UNSAFE_SCHEME)


def test_empty_href_is_same_host():
    decision = classify_redirect_trust("careers.acme.com", "")
    assert decision.trust == RedirectTrust.SAME_HOST


def test_file_scheme_is_trusted_local_fixture():
    """CLAUDE.md Phase 10 section 55: file:// is this project's entire local
    test-fixture mechanism -- a real live-Chromium test caught an earlier
    version of this module incorrectly treating it as UNSAFE_SCHEME, which
    broke every apply-entry browser fixture."""
    decision = classify_redirect_trust("", "file:///tmp/some/fixture/form.html")
    assert decision.trust == RedirectTrust.SAME_HOST


def test_mock_ats_domains_never_count_as_trusted():
    """The mock_ats fixture's local/test hosts must never be a real
    redirect-trust signal for a genuine external destination."""
    decision = classify_redirect_trust("careers.acme.com", "https://localhost/apply")
    assert decision.trust == RedirectTrust.UNTRUSTED


# --- resolve_application_url --------------------------------------------------

def test_resolve_prefers_canonical_url():
    resolved = resolve_application_url(canonical_url="https://boards.greenhouse.io/acme/jobs/1",
                                        job_url="https://acme.com/careers/1", provider="greenhouse")
    assert resolved.url == "https://boards.greenhouse.io/acme/jobs/1"
    assert resolved.provenance == UrlProvenance.DISCOVERY_PROVIDER


def test_resolve_falls_back_to_job_url():
    resolved = resolve_application_url(canonical_url="", job_url="https://acme.com/careers/1", provider="")
    assert resolved.url == "https://acme.com/careers/1"
    assert resolved.provenance == UrlProvenance.JOB_DETAIL


def test_resolve_empty_when_nothing_available():
    resolved = resolve_application_url(canonical_url="", job_url="", provider="")
    assert resolved.url == ""
    assert resolved.provenance == UrlProvenance.USER_PROVIDED
