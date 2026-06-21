"""Resource load-state arithmetic: canonical minimal descriptions, promotion, deltas, stores."""

import pytest

from rhizome.resources_new import (
    load_delta,
    normalize,
    ResourceContextStore,
    ResourceIndexStore,
    ResourceTree,
    ResourceTreeNode as N,
)


@pytest.fixture
def tree() -> ResourceTree:
    # Resource 1: sections 10, 11, 12; section 10 has children 101, 102.
    t = ResourceTree()
    t.load_rows([1], [(10, 1, None), (11, 1, None), (12, 1, None), (101, 1, 10), (102, 1, 10)])
    return t


R1, S10, S11, S12 = N("resource", 1), N("section", 10), N("section", 11), N("section", 12)
S101, S102 = N("section", 101), N("section", 102)


# ------------------------------------------------------------------------------------------------
# Tree
# ------------------------------------------------------------------------------------------------

def test_tree_structure(tree):
    assert tree.roots == (R1,)
    assert tree.parent(S101) == S10 and tree.parent(S10) == R1 and tree.parent(R1) is None
    assert tree.children(S10) == (S101, S102) and tree.children(S11) == ()
    assert S102 in tree and N("section", 999) not in tree
    assert tree.parent(N("section", 999)) is None and tree.children(N("section", 999)) == ()
    assert len(tree) == 6


async def test_tree_refresh_requires_factory(tree):
    with pytest.raises(RuntimeError):
        await tree.refresh()


# ------------------------------------------------------------------------------------------------
# Canonical form
# ------------------------------------------------------------------------------------------------

def test_normalize_promotes_and_minimizes(tree):
    # All children loaded -> parent stands in for them.
    assert normalize([S101, S102], tree) == {S10}
    # Everything loaded -> the root entry alone.
    assert normalize([S10, S11, S12], tree) == {R1}
    # Redundant child under a loaded parent disappears.
    assert normalize([S10, S101], tree) == {S10}
    # Stale ids survive normalization untouched (so deltas can emit their removal).
    stale = N("section", 999)
    assert stale in normalize([S11, stale], tree)


def test_store_load_unload_cascades(tree):
    store = ResourceContextStore(tree)

    assert store.set_loaded(S101, True) and store.loaded == {S101}
    assert store.is_loaded(S101) and not store.is_loaded(S10)

    # Sibling completes the parent -> promotion; walk-up sees through it.
    assert store.set_loaded(S102, True) and store.loaded == {S10}
    assert store.is_loaded(S101)

    # Remaining top-level sections complete the resource.
    store.set_loaded(S11, True)
    store.set_loaded(S12, True)
    assert store.loaded == {R1}

    # Unloading a grandchild demotes ancestors, siblings stay.
    assert store.set_loaded(S101, False)
    assert store.loaded == {S102, S11, S12}
    assert not store.is_loaded(S10) and not store.is_loaded(R1)

    # Unloading a partially-loaded parent clears its subtree (tri-state untick).
    assert store.set_loaded(S10, False)
    assert store.loaded == {S11, S12}

    # No-ops report no change.
    assert not store.set_loaded(S11, True)
    assert not store.set_loaded(S101, False)


def test_store_prune_after_tree_shrink(tree):
    store = ResourceContextStore(tree)
    store.set_loaded(S11, True)
    tree.load_rows([2], [])  # resource 1 deleted; refresh would do this
    assert store.prune() and store.loaded == frozenset()


def test_copy_from_preserves_identity(tree):
    a, b = ResourceContextStore(tree), ResourceContextStore(tree)
    a.set_loaded(S11, True)
    b.copy_from(a)
    assert b.loaded == a.loaded
    a.set_loaded(S12, True)
    assert b.loaded != a.loaded, "copy is by content, not by reference"
    with pytest.raises(ValueError):
        b.copy_from(ResourceContextStore(ResourceTree()))


# ------------------------------------------------------------------------------------------------
# Deltas & the index store
# ------------------------------------------------------------------------------------------------

def test_load_delta_is_entry_level(tree):
    delta = load_delta([S101], [S10])
    assert delta.additions == [S10] and delta.removals == [S101] and bool(delta)
    assert not load_delta([S11], [S11]), "canonical equality -> empty delta"


async def test_index_store_watermark(tree):
    index_store = ResourceIndexStore(tree)
    index_store.set_loaded(S101, True)

    # consume() is a command (returns None); the watermark it advances is observable via `consumed`.
    assert await index_store.consume() is None
    assert index_store.consumed == index_store.loaded == {S101}

    # A promotion between consumptions advances the watermark to the canonical (promoted) form.
    index_store.set_loaded(S102, True)                  # S101 + S102 -> promotes to S10
    await index_store.consume()
    assert index_store.consumed == index_store.loaded == {S10}


# ------------------------------------------------------------------------------------------------
# Content cache (opt-in; the shared global store turns it on, single-use local stores leave it off)
# ------------------------------------------------------------------------------------------------

async def test_context_store_caches_blocks_when_enabled(tree):
    store = ResourceContextStore(tree, cache=True)
    calls = 0

    async def build() -> str:
        nonlocal calls
        calls += 1
        return f"v{calls}"

    assert await store.block(1, build) == "v1"
    assert await store.block(1, build) == "v1", "served from cache; build not re-run"
    assert calls == 1

    # A load change invalidates the cache (entries are valid only for the current desired state).
    store.set_loaded(S11, True)
    assert await store.block(1, build) == "v2" and calls == 2


async def test_context_store_does_not_cache_by_default(tree):
    store = ResourceContextStore(tree)  # cache off
    calls = 0

    async def build() -> str:
        nonlocal calls
        calls += 1
        return "x"

    await store.block(1, build)
    await store.block(1, build)
    assert calls == 2, "no cache -> build runs every call"
