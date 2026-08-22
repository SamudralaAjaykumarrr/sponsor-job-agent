"""CLAUDE.md Phase 13 sections 4-10: formal multi-signal JobIdentityVerification
and bounded evidence persistence. Never touches network/browser -- pure
classification logic plus db_session()-backed evidence storage exercised via
tmp_env like every other persistence-layer test in this project."""

from app.applications.job_identity import (
    JobIdentitySignals,
    JobIdentityVerdict,
    list_verifications,
    meets_min_confidence,
    record_verification,
    verify_job_identity_full,
)


def test_no_comparable_signal_is_insufficient():
    result = verify_job_identity_full(JobIdentitySignals(), JobIdentitySignals())
    assert result.verdict == JobIdentityVerdict.INSUFFICIENT


def test_matching_requisition_id_alone_is_verified():
    stored = JobIdentitySignals(requisition_id="R-1234")
    observed = JobIdentitySignals(requisition_id="r-1234")
    result = verify_job_identity_full(stored, observed)
    assert result.verdict == JobIdentityVerdict.VERIFIED
    assert "requisition_id" in result.signals_matched


def test_mismatched_requisition_id_is_mismatch():
    stored = JobIdentitySignals(requisition_id="R-1234")
    observed = JobIdentitySignals(requisition_id="R-9999")
    result = verify_job_identity_full(stored, observed)
    assert result.verdict == JobIdentityVerdict.MISMATCH
    assert "requisition_id" in result.signals_mismatched


def test_mismatched_company_is_mismatch_even_if_title_matches():
    stored = JobIdentitySignals(title="Software Engineer", company="Acme Corp")
    observed = JobIdentitySignals(title="Software Engineer", company="Globex Inc")
    result = verify_job_identity_full(stored, observed)
    assert result.verdict == JobIdentityVerdict.MISMATCH
    assert "company" in result.signals_mismatched
    # title still agreed -- both facts are reported, not just the mismatch.
    assert "title" in result.signals_matched


def test_two_matching_signals_without_requisition_is_verified():
    stored = JobIdentitySignals(title="Backend Engineer", company="Acme Corp")
    observed = JobIdentitySignals(title="Backend Engineer", company="Acme Corp")
    result = verify_job_identity_full(stored, observed)
    assert result.verdict == JobIdentityVerdict.VERIFIED
    assert set(result.signals_matched) == {"title", "company"}


def test_single_matching_signal_is_probable_not_verified():
    stored = JobIdentitySignals(company="Acme Corp")
    observed = JobIdentitySignals(company="Acme Corp")
    result = verify_job_identity_full(stored, observed)
    assert result.verdict == JobIdentityVerdict.PROBABLE


def test_company_suffix_normalization_still_matches():
    stored = JobIdentitySignals(title="Engineer", company="Acme Corp, Inc.")
    observed = JobIdentitySignals(title="Engineer", company="acme corp")
    result = verify_job_identity_full(stored, observed)
    assert "company" in result.signals_matched


def test_title_variant_still_matches_via_normalization():
    stored = JobIdentitySignals(title="Senior Software Engineer", company="Acme")
    observed = JobIdentitySignals(title="Software Engineer, Senior", company="Acme")
    result = verify_job_identity_full(stored, observed)
    assert "title" in result.signals_matched
    assert result.verdict == JobIdentityVerdict.VERIFIED


def test_different_seniority_title_is_a_mismatch_signal():
    stored = JobIdentitySignals(title="Senior Software Engineer", company="Acme")
    observed = JobIdentitySignals(title="Software Engineer", company="Acme")
    result = verify_job_identity_full(stored, observed)
    assert "title" in result.signals_mismatched
    assert result.verdict == JobIdentityVerdict.MISMATCH


def test_requisition_extracted_from_url_when_not_explicit():
    stored = JobIdentitySignals(url="https://boards.greenhouse.io/acme/jobs/1234567")
    observed = JobIdentitySignals(url="https://boards.greenhouse.io/acme/jobs/1234567")
    result = verify_job_identity_full(stored, observed)
    assert result.verdict == JobIdentityVerdict.VERIFIED


def test_a_single_strong_signal_is_probable_not_ambiguous():
    # A single STRONG signal (provider, company, title, tenant, site) always
    # resolves to PROBABLE, never AMBIGUOUS -- AMBIGUOUS is reserved for
    # when only a WEAK, non-corroborating signal (location) is comparable.
    stored = JobIdentitySignals(provider="greenhouse")
    observed = JobIdentitySignals(provider="greenhouse")
    result = verify_job_identity_full(stored, observed)
    assert result.verdict == JobIdentityVerdict.PROBABLE


def test_location_only_match_is_ambiguous_not_probable():
    """CLAUDE.md Phase 13 section 4 lists location as a signal to verify --
    but two genuinely different requisitions commonly share an identical
    location string, so a location match ALONE is only ever AMBIGUOUS (weak,
    circumstantial evidence), never enough to be PROBABLE."""
    stored = JobIdentitySignals(location="Remote - US")
    observed = JobIdentitySignals(location="remote - us")
    result = verify_job_identity_full(stored, observed)
    assert result.verdict == JobIdentityVerdict.AMBIGUOUS
    assert "location" in result.signals_matched


def test_location_mismatch_never_causes_a_mismatch_verdict():
    """A location MISMATCH alone must never be treated as a contradiction --
    a posting can legitimately be listed under more than one location
    string -- so it must never, by itself, produce MISMATCH."""
    stored = JobIdentitySignals(location="Remote - US")
    observed = JobIdentitySignals(location="Austin, TX")
    result = verify_job_identity_full(stored, observed)
    assert result.verdict != JobIdentityVerdict.MISMATCH


def test_location_match_plus_one_strong_signal_is_still_verified_by_two():
    """A weak (location) match alongside a strong (company) match still
    counts location toward the reported matched signals, but VERIFIED still
    requires 2+ STRONG signals or a requisition id -- one strong + one weak
    is not enough (stays PROBABLE)."""
    stored = JobIdentitySignals(company="Acme Corp", location="Remote - US")
    observed = JobIdentitySignals(company="Acme Corp", location="Remote - US")
    result = verify_job_identity_full(stored, observed)
    assert result.verdict == JobIdentityVerdict.PROBABLE
    assert "location" in result.signals_matched
    assert "company" in result.signals_matched


def test_record_and_list_verification(tmp_env):
    stored = JobIdentitySignals(title="Engineer", company="Acme", url="https://x/jobs/999999")
    observed = JobIdentitySignals(title="Engineer", company="Globex", url="https://x/jobs/888888")
    verification = verify_job_identity_full(stored, observed)
    row = record_verification(42, stage="PRE_UPLOAD", stored=stored, observed=observed, verification=verification)
    assert row["job_id"] == 42
    assert row["result"] == "MISMATCH"
    assert row["stage"] == "PRE_UPLOAD"

    rows = list_verifications(job_id=42)
    assert len(rows) == 1
    assert rows[0]["stored_company"] == "Acme"
    assert rows[0]["observed_company"] == "Globex"


def test_list_verifications_filters_by_job(tmp_env):
    verification = verify_job_identity_full(JobIdentitySignals(), JobIdentitySignals())
    record_verification(1, stage="PRE_UPLOAD", stored=JobIdentitySignals(), observed=JobIdentitySignals(),
                         verification=verification)
    record_verification(2, stage="PRE_UPLOAD", stored=JobIdentitySignals(), observed=JobIdentitySignals(),
                         verification=verification)
    assert len(list_verifications(job_id=1)) == 1
    assert len(list_verifications()) == 2


def test_record_verification_stores_no_pii_columns(tmp_env):
    """CLAUDE.md Phase 13 section 5: every column is already-public
    job-posting metadata -- never a candidate value."""
    verification = verify_job_identity_full(JobIdentitySignals(), JobIdentitySignals())
    row = record_verification(1, stage="PRE_FINAL_SUBMIT", stored=JobIdentitySignals(title="Engineer"),
                               observed=JobIdentitySignals(), verification=verification)
    forbidden = {"email", "phone", "ssn", "password", "resume"}
    assert not (forbidden & set(row.keys()))


# --- CLAUDE.md Phase 13 acceptance correction: meets_min_confidence() -------
# gates unattended continuation for ALL FIVE verdicts. Default floor is
# VERIFIED -- only VERIFIED passes; PROBABLE/AMBIGUOUS/INSUFFICIENT/MISMATCH
# all fail. An operator may explicitly lower the floor (never MISMATCH).

def test_verified_meets_default_confidence():
    assert meets_min_confidence(JobIdentityVerdict.VERIFIED, "VERIFIED") is True


def test_probable_fails_default_confidence():
    assert meets_min_confidence(JobIdentityVerdict.PROBABLE, "VERIFIED") is False


def test_ambiguous_fails_default_confidence():
    assert meets_min_confidence(JobIdentityVerdict.AMBIGUOUS, "VERIFIED") is False


def test_insufficient_fails_default_confidence():
    assert meets_min_confidence(JobIdentityVerdict.INSUFFICIENT, "VERIFIED") is False


def test_mismatch_always_fails_regardless_of_configured_floor():
    """CLAUDE.md Phase 13 acceptance correction: a confirmed contradiction
    is never configurable -- it fails even the loosest floor."""
    assert meets_min_confidence(JobIdentityVerdict.MISMATCH, "INSUFFICIENT") is False


def test_probable_passes_when_floor_explicitly_lowered():
    """An operator may explicitly accept PROBABLE as sufficient -- a
    deliberate, documented risk acceptance, never the silent default."""
    assert meets_min_confidence(JobIdentityVerdict.PROBABLE, "PROBABLE") is True
    assert meets_min_confidence(JobIdentityVerdict.AMBIGUOUS, "PROBABLE") is False


def test_unknown_configured_value_defaults_to_strictest_floor():
    """A malformed/unrecognized config value must never silently loosen the
    gate -- falls back to the strictest (VERIFIED) floor."""
    assert meets_min_confidence(JobIdentityVerdict.PROBABLE, "not-a-real-value") is False
    assert meets_min_confidence(JobIdentityVerdict.VERIFIED, "not-a-real-value") is True
