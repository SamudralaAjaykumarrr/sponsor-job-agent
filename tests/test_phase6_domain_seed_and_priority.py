"""CLAUDE.md Phase 6 sections 25-26: domain-seed acquisition pipeline
(company_name + company_domain -> bounded discovery -> ATS detect -> tenant
extraction -> candidate portal) and deterministic acquisition-priority
scoring. page_discovery.discover_career_links() itself (the bounded HTTP
crawl) already has its own Phase 4 test coverage; these tests verify the
Phase 6 WIRING on top of it, so discover_career_links is monkeypatched
directly rather than re-mocking HTTP through it."""

from dataclasses import dataclass

import pytest

from app.providers.capabilities import SupportLevel
from app.providers.detector import DetectionResult
from app.registry import domain_seed, store
from app.registry.acquisition_priority import PriorityInputs, compute_priority
from app.registry.page_discovery import DiscoveryResult


def _fake_discovery_with_match(domain: str) -> DiscoveryResult:
    result = DiscoveryResult(domain=domain)
    result.pages_fetched = 3
    result.best_match = DetectionResult(
        provider="greenhouse", confidence=0.9, tenant_identifier="acme", evidence="matched greenhouse URL pattern",
    )
    result.best_match_url = f"https://boards.greenhouse.io/acme"
    return result


def _fake_discovery_no_match(domain: str) -> DiscoveryResult:
    result = DiscoveryResult(domain=domain)
    result.pages_fetched = 4
    return result


def test_build_candidate_uses_detected_provider_and_tenant(monkeypatch):
    monkeypatch.setattr(domain_seed, "discover_career_links", _fake_discovery_with_match)
    candidate, discovery = domain_seed.build_candidate_from_domain("Acme Corp", "acme.example.com", row_number=1)
    assert candidate.provider == "greenhouse"
    assert candidate.tenant_identifier == "acme"
    assert candidate.careers_url
    assert discovery.pages_fetched == 3


def test_build_candidate_falls_back_to_bare_company_when_no_match(monkeypatch):
    monkeypatch.setattr(domain_seed, "discover_career_links", _fake_discovery_no_match)
    candidate, discovery = domain_seed.build_candidate_from_domain("Mystery Co", "mystery.example.com", row_number=2)
    assert candidate.provider == ""
    assert candidate.tenant_identifier == ""
    assert candidate.company_name == "Mystery Co"


def test_run_domain_seed_batch_creates_candidate_portal(tmp_env, monkeypatch):
    monkeypatch.setattr(domain_seed, "discover_career_links", _fake_discovery_with_match)
    result = domain_seed.run_domain_seed_batch([("Acme Corp", "acme.example.com")], source_name="test-domain-seed")

    assert result.companies_discovered_ats == 1
    assert result.companies_no_match == 0
    assert result.rows_invalid == 0
    assert result.rows[0].portal_id is not None

    portal = store.get_portal(result.rows[0].portal_id)
    assert portal.provider == "greenhouse"
    assert portal.tenant_identifier == "acme"
    assert portal.verification_status.value in ("DISCOVERED", "CANDIDATE")  # never auto-VERIFIED/ACTIVE


def test_run_domain_seed_batch_one_bad_domain_does_not_abort_others(tmp_env, monkeypatch):
    call_log = []

    def flaky_discover(domain: str) -> DiscoveryResult:
        call_log.append(domain)
        if domain == "broken.example.com":
            raise RuntimeError("simulated discovery crash")
        return _fake_discovery_no_match(domain)

    monkeypatch.setattr(domain_seed, "discover_career_links", flaky_discover)
    result = domain_seed.run_domain_seed_batch([
        ("Broken Co", "broken.example.com"),
        ("Fine Co", "fine.example.com"),
    ])

    assert len(call_log) == 2, "second domain must still be attempted after the first raised"
    assert result.rows_invalid == 1
    assert len(result.rows) == 2


# --- acquisition priority ----------------------------------------------------

def test_priority_never_uses_interview_probability_field():
    # Structural guarantee: no such field exists on the inputs dataclass at all.
    assert "interview_probability" not in PriorityInputs.__dataclass_fields__


def test_higher_support_level_scores_higher():
    full = compute_priority(PriorityInputs(support_level=SupportLevel.FULL))
    unsupported = compute_priority(PriorityInputs(support_level=SupportLevel.UNSUPPORTED))
    assert full.score > unsupported.score


def test_sponsorship_history_signal_raises_priority_but_reason_says_priority_only():
    with_signal = compute_priority(PriorityInputs(has_sponsorship_history_signal=True))
    without_signal = compute_priority(PriorityInputs(has_sponsorship_history_signal=False))
    assert with_signal.score > without_signal.score
    assert any("ACQUISITION PRIORITY ONLY" in r for r in with_signal.reasons)


def test_priority_combines_multiple_signals_additively():
    result = compute_priority(PriorityInputs(
        is_us_employer=True, is_technical_employer=True, known_technology_hiring=True,
        support_level=SupportLevel.FULL, historical_job_yield=50, registry_confidence=80,
        has_sponsorship_history_signal=True,
    ))
    zero = compute_priority(PriorityInputs())
    assert result.score > zero.score
    assert len(result.reasons) == 7
