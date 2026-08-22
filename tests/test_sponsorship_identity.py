"""CLAUDE.md Phase 7 sections 8-10, 36, 44: employer identity resolution,
aliases, parent/subsidiary safety. Never merge on name similarity alone."""

from app.registry.models import Company
from app.registry import store
from app.sponsorship.aliases import add_alias, find_company_id_by_alias, list_alias_collisions
from app.sponsorship.identity import list_pending_reviews, resolve_company, resolve_review
from app.sponsorship.relationships import add_relationship, find_contradictions, list_relationships_for_company
from app.sponsorship.schema import AliasType, RelationshipType


def _mk_company(name, domain=""):
    return store.insert_company(Company(normalized_name=name, display_name=name, primary_domain=domain))


def test_domain_match_resolves_unambiguously(tmp_env):
    cid = _mk_company("google", "google.com")
    match = resolve_company("Google LLC", "google.com")
    assert match.company_id == cid
    assert match.matched_via == "domain"


def test_alias_match_resolves_google_llc_to_google(tmp_env):
    cid = _mk_company("google", "google.com")
    add_alias(cid, "Google LLC", AliasType.LEGAL_NAME, verified=True)
    match = resolve_company("Google LLC")
    assert match.company_id == cid
    assert match.matched_via == "alias"


def test_similar_unrelated_company_names_are_not_merged(tmp_env):
    """'Acme Corp' and 'Acme Corp of Texas' must not silently merge just
    because their names look similar -- only an exact normalized-name or
    domain/alias match may resolve."""
    _mk_company("acme corp", "acme.com")
    match = resolve_company("Acme Corp of Texas", "acmetexas.com")
    assert match.company_id is None
    assert match.matched_via == "none"


def test_ambiguous_same_name_different_domains_goes_to_review(tmp_env):
    _mk_company("acme", "acme-east.com")
    _mk_company("acme", "acme-west.com")
    match = resolve_company("Acme")
    assert match.company_id is None
    assert match.matched_via == "ambiguous"
    pending = list_pending_reviews()
    assert len(pending) == 1
    assert len(pending[0]["candidate_company_ids"]) == 2


def test_resolve_review_attaches_company(tmp_env):
    c1 = _mk_company("acme", "acme-east.com")
    _mk_company("acme", "acme-west.com")
    resolve_company("Acme")
    pending = list_pending_reviews()
    resolve_review(pending[0]["id"], c1, note="confirmed east entity")
    assert len(list_pending_reviews()) == 0


def test_renamed_company_via_former_name_alias(tmp_env):
    cid = _mk_company("newbrand", "newbrand.com")
    add_alias(cid, "OldBrand Inc", AliasType.FORMER_NAME, verified=True)
    match = resolve_company("OldBrand Inc")
    assert match.company_id == cid


def test_acquired_company_relationship_stored_not_merged(tmp_env):
    parent = _mk_company("bigco", "bigco.com")
    acquired = _mk_company("smallco", "smallco.com")
    add_relationship(parent, acquired, RelationshipType.ACQUIRED, confidence=80, verified=True)
    rels = list_relationships_for_company(parent)
    assert len(rels) == 1
    assert rels[0]["relationship_type"] == "ACQUIRED"
    # Identity resolution must still treat them as two distinct companies.
    assert resolve_company("SmallCo", "smallco.com").company_id == acquired
    assert resolve_company("BigCo", "bigco.com").company_id == parent


def test_same_company_multiple_domains_via_alias(tmp_env):
    cid = _mk_company("multico", "multico.com")
    add_alias(cid, "multico.io", AliasType.BRAND_NAME, verified=True)
    # The alias is on the name, domain-based lookup for a second domain still
    # requires an explicit alias/domain row -- verifies no accidental magic.
    assert find_company_id_by_alias("multico.io") == cid


def test_alias_collision_detected(tmp_env):
    c1 = _mk_company("companyone", "one.com")
    c2 = _mk_company("companytwo", "two.com")
    add_alias(c1, "SharedBrand", AliasType.BRAND_NAME, verified=True)
    add_alias(c2, "SharedBrand", AliasType.BRAND_NAME, verified=True)
    collisions = list_alias_collisions()
    assert len(collisions) == 1
    assert collisions[0]["n"] == 2


def test_parent_subsidiary_contradiction_detected(tmp_env):
    a = _mk_company("companya", "a.com")
    b = _mk_company("companyb", "b.com")
    add_relationship(a, b, RelationshipType.PARENT, verified=True)
    add_relationship(b, a, RelationshipType.PARENT, verified=True)
    contradictions = find_contradictions()
    assert len(contradictions) == 1


def test_relationship_rejects_self_reference(tmp_env):
    a = _mk_company("selfco", "self.com")
    import pytest

    with pytest.raises(ValueError):
        add_relationship(a, a, RelationshipType.SUBSIDIARY)
