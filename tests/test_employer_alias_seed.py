"""Sponsorship Intelligence Coverage V1: verified alias/identity seed loaders.
Test matrix item B (normalized legal-suffix match) and item C (known alias
mapping) exercised end-to-end through the real seed-file format."""

import json

from app.registry.models import Company
from app.registry import store
from app.sponsorship.aliases import find_company_id_by_alias, seed_known_aliases
from app.sponsorship.identity import resolve_company
from app.sponsorship.registry_backfill import seed_missing_employer_identities


def _write_alias_seed(tmp_path, entries):
    path = tmp_path / "aliases.json"
    path.write_text(json.dumps({"aliases": entries}), encoding="utf-8")
    return path


def test_seed_applies_verified_alias(tmp_env, tmp_path):
    cid = store.insert_company(Company(normalized_name="ramp", display_name="Ramp", primary_domain="ramp.com"))
    path = _write_alias_seed(tmp_path, [
        {"registry_normalized_name": "ramp", "alias": "Ramp Business Corporation",
         "alias_type": "LEGAL_NAME", "source": "test", "confidence": 95},
    ])
    result = seed_known_aliases(path)
    assert result.applied == 1
    assert find_company_id_by_alias("Ramp Business Corporation") == cid
    # item B: the resolved alias makes the real legal-entity name resolvable end to end.
    assert resolve_company("Ramp Business Corporation").company_id == cid


def test_seed_skips_when_registry_company_missing(tmp_env, tmp_path):
    path = _write_alias_seed(tmp_path, [
        {"registry_normalized_name": "doesnotexist", "alias": "Some Legal Name Inc",
         "alias_type": "LEGAL_NAME", "source": "test", "confidence": 90},
    ])
    result = seed_known_aliases(path)
    assert result.applied == 0
    assert result.skipped_no_company == ["Some Legal Name Inc"]


def test_seed_skips_ambiguous_registry_company(tmp_env, tmp_path):
    store.insert_company(Company(normalized_name="dupe", display_name="Dupe East", primary_domain="dupe-east.com"))
    store.insert_company(Company(normalized_name="dupe", display_name="Dupe West", primary_domain="dupe-west.com"))
    path = _write_alias_seed(tmp_path, [
        {"registry_normalized_name": "dupe", "alias": "Dupe Legal Inc", "alias_type": "LEGAL_NAME"},
    ])
    result = seed_known_aliases(path)
    assert result.applied == 0
    assert result.skipped_ambiguous_company == ["Dupe Legal Inc"]


def test_seed_is_idempotent(tmp_env, tmp_path):
    store.insert_company(Company(normalized_name="notion", display_name="Notion", primary_domain="notion.so"))
    path = _write_alias_seed(tmp_path, [
        {"registry_normalized_name": "notion", "alias": "Notion Labs Inc", "alias_type": "LEGAL_NAME"},
    ])
    seed_known_aliases(path)
    result2 = seed_known_aliases(path)
    assert result2.applied == 1  # add_alias itself is an idempotent upsert
    from app.sponsorship.aliases import list_aliases_for_company

    cid = resolve_company("Notion Labs Inc").company_id
    assert len(list_aliases_for_company(cid)) == 1


def test_missing_seed_file_is_a_safe_noop(tmp_env, tmp_path):
    result = seed_known_aliases(tmp_path / "does-not-exist.json")
    assert result.applied == 0


def test_registry_identity_backfill_creates_missing_company(tmp_env, tmp_path):
    path = tmp_path / "identities.json"
    path.write_text(json.dumps({
        "companies": [
            {"normalized_name": "anthropic", "display_name": "Anthropic",
             "primary_domain": "anthropic.com", "careers_home_url": "https://www.anthropic.com/careers",
             "country": "US"},
        ]
    }), encoding="utf-8")
    result = seed_missing_employer_identities(path)
    assert result.created == 1
    match = resolve_company("Anthropic", "anthropic.com")
    assert match.company_id is not None
    assert match.matched_via == "domain"


def test_registry_identity_backfill_is_idempotent(tmp_env, tmp_path):
    path = tmp_path / "identities.json"
    path.write_text(json.dumps({
        "companies": [{"normalized_name": "pump", "display_name": "Pump.co", "primary_domain": "pump.co"}]
    }), encoding="utf-8")
    r1 = seed_missing_employer_identities(path)
    r2 = seed_missing_employer_identities(path)
    assert r1.created == 1
    assert r2.created == 0
    assert r2.already_present == ["Pump.co"]
