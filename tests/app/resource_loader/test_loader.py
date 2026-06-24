"""ResourceLoaderModel — the two-axis load-state controller over the resources stores.

Most tests run against an in-memory ``ResourceTree`` (no DB) and exercise the store-writing logic
directly; the display metadata they need is seeded straight onto the VM (``_seed``). One DB-backed
test covers the ``load`` path end to end (skeleton + metadata + topic links).
"""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from rhizome.app.resource_loader import ContextScope, ResourceLoaderModel
from rhizome.db.models import Base, Resource, ResourceSection, Topic, TopicResource
from rhizome.resources import (
    ResourceContextStore,
    ResourceIndexStore,
    ResourceTree,
    ResourceTreeNode as N,
)


# Resource 1: sections 10 (⊃ 101, 102), 11, 12. Resource 2: no sections.
R1, R2 = N("resource", 1), N("resource", 2)
S10, S11, S12, S101, S102 = (N("section", i) for i in (10, 11, 12, 101, 102))


@pytest.fixture
def tree() -> ResourceTree:
    t = ResourceTree()
    t.load_rows([1, 2], [(10, 1, None), (11, 1, None), (12, 1, None), (101, 1, 10), (102, 1, 10)])
    return t


def make_loader(tree: ResourceTree, *, session_factory=None, index=True, glob=True, local=True) -> ResourceLoaderModel:
    return ResourceLoaderModel(
        session_factory,
        tree,
        index=ResourceIndexStore(tree) if index else None,
        global_context=ResourceContextStore(tree) if glob else None,
        local_context=ResourceContextStore(tree) if local else None,
    )


def _seed(vm, *, metadata=None, titles=None, topics=None) -> None:
    """Inject the display data ``load`` would normally fetch, then rebuild the forest."""
    if metadata is not None:
        vm._metadata = metadata
    if titles is not None:
        vm._section_titles = titles
    if topics is not None:
        vm._resource_topics = {rid: frozenset(ts) for rid, ts in topics.items()}
    vm._rebuild_forest()


class Rec:
    """Records the VM's emitted callbacks (subscribers are weakly held, so keep this alive)."""

    def __init__(self, vm) -> None:
        self.data: list[bool] = []
        self.load: list[int | None] = []
        vm.subscribe(vm.Callbacks.OnDataChanged, self._on_data)
        vm.subscribe(vm.Callbacks.OnLoadStateChanged, self._on_load)

    def _on_data(self) -> None:
        self.data.append(True)

    def _on_load(self, rid) -> None:
        self.load.append(rid)


# ------------------------------------------------------------------------------------------------
# Index axis
# ------------------------------------------------------------------------------------------------

def test_toggle_index_on_off(tree):
    vm = make_loader(tree)
    assert vm.node_state(R1).indexed is False
    vm.toggle_index(R1)
    assert vm.node_state(R1).indexed is True
    vm.toggle_index(R1)
    assert vm.node_state(R1).indexed is False


def test_index_walks_up_to_descendants(tree):
    vm = make_loader(tree)
    vm.toggle_index(R1)
    # Loading the resource covers its whole subtree (walk-up coverage).
    assert vm.node_state(S10).indexed and vm.node_state(S101).indexed
    # But loading a section does NOT make the resource itself read loaded.
    vm2 = make_loader(tree)
    vm2.toggle_index(S10)
    assert vm2.node_state(S10).indexed and not vm2.node_state(R1).indexed
    assert vm2.node_state(S101).indexed and not vm2.node_state(S11).indexed


def test_index_independent_of_context(tree):
    vm = make_loader(tree)
    vm.toggle_index(R1)
    vm.cycle_context(R1)  # NONE -> LOCAL
    state = vm.node_state(R1)
    assert state.indexed is True and state.context is ContextScope.LOCAL


# ------------------------------------------------------------------------------------------------
# Context axis (NONE -> LOCAL -> GLOBAL -> NONE, mutually exclusive channels)
# ------------------------------------------------------------------------------------------------

def test_context_cycle_and_mutex(tree):
    vm = make_loader(tree)
    assert vm.node_state(R1).context is ContextScope.NONE
    vm.cycle_context(R1)
    assert vm.node_state(R1).context is ContextScope.LOCAL
    vm.cycle_context(R1)
    # Advancing to GLOBAL clears the LOCAL channel — one context channel per node at a time.
    assert vm.node_state(R1).context is ContextScope.GLOBAL
    assert not vm._local.is_loaded(R1) and vm._global.is_loaded(R1)
    vm.cycle_context(R1)
    assert vm.node_state(R1).context is ContextScope.NONE
    assert not vm._global.is_loaded(R1) and not vm._local.is_loaded(R1)


def test_cycle_skips_unavailable_local(tree):
    vm = make_loader(tree, local=False)  # NONE -> GLOBAL -> NONE
    assert not vm.local_context_available
    vm.cycle_context(R1)
    assert vm.node_state(R1).context is ContextScope.GLOBAL
    vm.cycle_context(R1)
    assert vm.node_state(R1).context is ContextScope.NONE


def test_toggle_context_direct_and_mutex(tree):
    vm = make_loader(tree)
    vm.toggle_context(R1, ContextScope.GLOBAL)
    assert vm.node_state(R1).context is ContextScope.GLOBAL
    vm.toggle_context(R1, ContextScope.LOCAL)  # switching channels clears the other (mutex)
    assert vm.node_state(R1).context is ContextScope.LOCAL and not vm._global.is_loaded(R1)
    vm.toggle_context(R1, ContextScope.LOCAL)  # toggling the same scope clears it
    assert vm.node_state(R1).context is ContextScope.NONE


def test_toggle_context_inert_when_unwired(tree):
    vm = make_loader(tree, glob=False)
    rec = Rec(vm)
    vm.toggle_context(R1, ContextScope.GLOBAL)  # no global store -> inert
    assert vm.node_state(R1).context is ContextScope.NONE and rec.load == []


# ------------------------------------------------------------------------------------------------
# Honest dashes — missing stores
# ------------------------------------------------------------------------------------------------

def test_no_stores_inert_and_unavailable(tree):
    vm = make_loader(tree, index=False, glob=False, local=False)
    assert not (vm.index_available or vm.global_context_available or vm.local_context_available)
    rec = Rec(vm)
    vm.toggle_index(R1)
    vm.cycle_context(R1)
    assert vm.node_state(R1) == vm.node_state(R2)  # still all-off everywhere
    assert vm.node_state(R1).indexed is False and vm.node_state(R1).context is ContextScope.NONE
    assert rec.load == []  # inert gestures emit nothing


def test_partial_store_availability(tree):
    vm = make_loader(tree, index=True, glob=False, local=False)
    assert vm.index_available and not vm.global_context_available
    vm.cycle_context(R1)  # no context stores -> inert
    assert vm.node_state(R1).context is ContextScope.NONE


# ------------------------------------------------------------------------------------------------
# Local-store swap
# ------------------------------------------------------------------------------------------------

def test_set_local_context_store_swaps_and_rebuilds(tree):
    vm = make_loader(tree)
    vm.cycle_context(R1)  # LOCAL on the current local store
    assert vm.node_state(R1).context is ContextScope.LOCAL

    fresh = ResourceContextStore(tree)  # a different leaf's local store — R1 not loaded there
    rec = Rec(vm)
    vm.set_local_context_store(fresh)
    assert vm.node_state(R1).context is ContextScope.NONE
    assert rec.data  # swap triggers a rebuild
    # Re-pointing at the same store is a no-op.
    rec.data.clear()
    vm.set_local_context_store(fresh)
    assert rec.data == []


# ------------------------------------------------------------------------------------------------
# Filters (visibility only)
# ------------------------------------------------------------------------------------------------

@pytest.fixture
def seeded(tree) -> ResourceLoaderModel:
    vm = make_loader(tree)
    _seed(
        vm,
        metadata={1: ("Calculus", 1200), 2: ("Linear Algebra", 800)},
        titles={10: "Limits", 11: "Derivatives", 12: "Integrals", 101: "Epsilon", 102: "Delta"},
        topics={1: {7}, 2: {8}},
    )
    return vm


def test_search_filters_by_name(seeded):
    assert [r.label for r in seeded.roots] == ["Calculus", "Linear Algebra"]
    seeded.set_search("calc")
    assert [r.label for r in seeded.roots] == ["Calculus"]
    seeded.set_search("nope")
    assert seeded.roots == []
    seeded.set_search("")
    assert len(seeded.roots) == 2


def test_topic_filter_by_link(seeded):
    seeded.set_topic_filter({7})
    assert [r.label for r in seeded.roots] == ["Calculus"]
    seeded.set_topic_filter({8})
    assert [r.label for r in seeded.roots] == ["Linear Algebra"]
    seeded.set_topic_filter(set())  # empty == no filter
    assert len(seeded.roots) == 2


def test_search_and_topic_are_anded(seeded):
    seeded.set_topic_filter({7})
    seeded.set_search("linear")  # matches R2, but R2 isn't in topic 7
    assert seeded.roots == []


def test_filters_do_not_touch_load_state(seeded):
    seeded.toggle_index(R1)
    seeded.set_search("nope")  # hides everything
    assert seeded._index.is_loaded(R1)  # load state survives a filter that hides the node
    seeded.set_search("")
    assert seeded.node_state(R1).indexed


def test_forest_structure_and_labels(seeded):
    calc = seeded.roots[0]
    assert calc.is_resource and calc.estimated_tokens == 1200 and calc.section_count == 5
    assert [c.label for c in calc.children] == ["Limits", "Derivatives", "Integrals"]
    limits = calc.children[0]
    assert [c.label for c in limits.children] == ["Epsilon", "Delta"]


# ------------------------------------------------------------------------------------------------
# Stats + callbacks
# ------------------------------------------------------------------------------------------------

def test_stats_count_resources_per_axis(tree):
    vm = make_loader(tree)
    vm.toggle_index(S10)  # a section load still tallies its owning resource (and its sections)
    vm.cycle_context(R2)  # NONE -> LOCAL on R2
    stats = vm.stats()
    assert stats.total_resources == 2 and stats.visible_resources == 2
    assert stats.indexed.resources == 1 and stats.indexed.sections == 3       # S10 + S101 + S102
    assert stats.context_local.resources == 1 and stats.context_global.resources == 0


def test_stats_axis_sections_and_tokens(seeded):
    seeded.toggle_index(R1)  # whole resource: all 5 sections covered, full token weight
    stats = seeded.stats()
    assert stats.indexed.resources == 1 and stats.indexed.sections == 5 and stats.indexed.tokens == 1200


def test_stats_visible_resources_tracks_filter(seeded):
    assert seeded.stats().visible_resources == 2  # no filter -> whole library visible
    seeded.set_search("calc")
    stats = seeded.stats()
    assert stats.total_resources == 2 and stats.visible_resources == 1


def test_load_state_changed_reports_owning_resource(tree):
    vm = make_loader(tree)
    rec = Rec(vm)
    vm.toggle_index(S101)  # deep section -> owning resource id
    assert rec.load == [1]
    vm.cycle_context(R2)
    assert rec.load == [1, 2]


def test_filter_changes_emit_data_changed(seeded):
    rec = Rec(seeded)
    seeded.set_search("calc")
    seeded.set_topic_filter({7})
    assert len(rec.data) == 2
    # Equality-guarded: re-applying the same filter emits nothing.
    seeded.set_search("calc")
    assert len(rec.data) == 2


# ------------------------------------------------------------------------------------------------
# DB-backed load()
# ------------------------------------------------------------------------------------------------

@pytest.fixture
async def db_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Resource(id=1, name="Calculus", estimated_tokens=1200))
        s.add(Resource(id=2, name="Linear Algebra", estimated_tokens=800))
        s.add(ResourceSection(id=10, resource_id=1, parent_id=None, title="Limits", depth=0, position=0))
        s.add(Topic(id=7, name="Math"))
        s.add(TopicResource(topic_id=7, resource_id=1))
        await s.commit()
    return factory


async def test_load_fetches_structure_metadata_and_links(db_factory):
    tree = ResourceTree(db_factory)
    vm = make_loader(tree, session_factory=db_factory)
    await vm.load()

    assert [r.label for r in vm.roots] == ["Calculus", "Linear Algebra"]
    calc = vm.roots[0]
    assert calc.estimated_tokens == 1200 and [c.label for c in calc.children] == ["Limits"]

    vm.set_topic_filter({7})
    assert [r.label for r in vm.roots] == ["Calculus"]
    vm.set_topic_filter(set())
    vm.set_search("linear")
    assert [r.label for r in vm.roots] == ["Linear Algebra"]
