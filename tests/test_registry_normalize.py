from app.registry.normalize import normalize_company_name, normalize_domain
from app.registry.url_canon import canonicalize_portal_url, is_valid_http_url


def test_normalize_company_name_strips_legal_suffixes():
    assert normalize_company_name("Acme, Inc.") == "acme"
    assert normalize_company_name("ACME WIDGETS LLC") == "acme widgets"
    assert normalize_company_name("Acme Corp.") == "acme"
    assert normalize_company_name("Acme   Corporation") == "acme"
    assert normalize_company_name("Acme Ltd.") == "acme"


def test_normalize_company_name_does_not_overstrip_generic_words():
    assert normalize_company_name("Acme Widgets") == "acme widgets"
    assert normalize_company_name("Acme") == "acme"


def test_normalize_company_name_whitespace_and_case_insensitive():
    assert normalize_company_name("  acme   corp  ") == "acme"
    assert normalize_company_name("ACME") == normalize_company_name("acme")


def test_normalize_domain_strips_scheme_www_path_trailing_slash():
    assert normalize_domain("https://www.Acme.com/careers/") == "acme.com"
    assert normalize_domain("http://acme.com") == "acme.com"
    assert normalize_domain("acme.com") == "acme.com"
    assert normalize_domain("WWW.ACME.COM") == "acme.com"


def test_normalize_domain_empty_input():
    assert normalize_domain("") == ""
    assert normalize_domain(None) == ""


def test_similar_names_different_domains_stay_distinguishable():
    # Normalization is only for display/grouping -- identity dedup in
    # store.py always requires (normalized_name, primary_domain) together, so
    # these two remain distinct companies even though names normalize equal.
    assert normalize_company_name("Acme Inc.") == normalize_company_name("Acme LLC")
    assert normalize_domain("acme.com") != normalize_domain("acme.io")


def test_canonicalize_portal_url_strips_tracking_params_and_trailing_slash():
    url = "https://boards.greenhouse.io/Acme/jobs/123/?utm_source=x&gh_jid=123"
    canon = canonicalize_portal_url(url)
    assert "utm_source" not in canon
    assert "gh_jid=123" in canon
    assert not canon.endswith("/123/")


def test_canonicalize_portal_url_preserves_tenant_path():
    # Workday tenant/site identifiers live in the path -- must be preserved exactly.
    url = "https://acme.wd5.myworkdayjobs.com/en-US/External"
    assert canonicalize_portal_url(url) == "https://acme.wd5.myworkdayjobs.com/en-US/External"


def test_canonicalize_portal_url_lowercases_host_strips_www():
    assert canonicalize_portal_url("https://WWW.Acme.com/careers") == "https://acme.com/careers"


def test_canonicalize_portal_url_empty_and_invalid():
    assert canonicalize_portal_url("") == ""
    assert canonicalize_portal_url("not a url") == ""


def test_is_valid_http_url():
    assert is_valid_http_url("https://acme.com/careers")
    assert not is_valid_http_url("not a url")
    assert not is_valid_http_url("")
    assert not is_valid_http_url("ftp://acme.com")
