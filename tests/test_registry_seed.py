from app.registry.repo import list_entries, seed_demo_entries


def test_seed_demo_entries_populates_when_empty(tmp_env):
    assert list_entries() == []
    seed_demo_entries()
    entries = list_entries()
    assert len(entries) >= 1


def test_seed_demo_entries_is_idempotent(tmp_env):
    seed_demo_entries()
    first_count = len(list_entries())
    seed_demo_entries()
    assert len(list_entries()) == first_count
