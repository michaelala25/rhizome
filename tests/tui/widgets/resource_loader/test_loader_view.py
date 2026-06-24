"""ResourceLoader view: a headless mount smoke test.

The VM logic is covered in ``tests/app/resource_loader``; this pins down the view wiring with a minimal
``run_test`` harness — mount the panel over a DB-backed VM, confirm it composes and the trees paint,
then drive the index/context keys and the topic filter and watch the glyphs and visible set respond.
"""

from rich.style import Style
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from textual.app import App, ComposeResult

from rhizome.app.resource_loader import ResourceLoaderModel
from rhizome.db.models import Base, Resource, ResourceSection, Topic, TopicResource
from rhizome.resources import (
    ResourceContextStore,
    ResourceIndexStore,
    ResourceTree,
    ResourceTreeNode as N,
)
from rhizome.tui.widgets.resource_loader import ResourceLoader, ResourceLoaderTree, TopicTree


async def _make_factory():
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


async def _make_vm():
    factory = await _make_factory()
    tree = ResourceTree(factory)
    return ResourceLoaderModel(
        factory,
        tree,
        index=ResourceIndexStore(tree),
        global_context=ResourceContextStore(tree),
        local_context=ResourceContextStore(tree),
    )


class _Harness(App):
    def __init__(self, vm: ResourceLoaderModel) -> None:
        super().__init__()
        self._vm = vm

    def compose(self) -> ComposeResult:
        yield ResourceLoader(self._vm)


async def test_loader_panel_mounts_renders_and_responds():
    vm = await _make_vm()
    async with _Harness(vm).run_test() as pilot:
        await pilot.pause()
        await pilot.pause()  # let load() resolve and the deferred cursor move land

        loader = pilot.app.query_one(ResourceLoader)
        tree = loader.query_one(ResourceLoaderTree)
        topics = loader.query_one(TopicTree)

        # The whole library renders (two resources), and on_mount focuses the resource tree.
        assert len(tree.root.children) == 2
        assert tree.has_focus and tree.cursor_node is tree.root.children[0]

        # space → toggle index on the cursor resource (Calculus); glyph reflects it.
        await pilot.press("space")
        assert vm._index.is_loaded(N("resource", 1))
        assert tree._glyph(N("resource", 1), Style()).plain == "[I]"

        # ctrl+enter cycles context NONE -> LOCAL (index stays on — the axes are independent).
        tree.action_cycle_context()
        await pilot.pause()
        assert vm._local.is_loaded(N("resource", 1))
        assert tree._glyph(N("resource", 1), Style()).plain == "[I|L]"

        # g toggles global context, switching the local channel off (mutually exclusive).
        await pilot.press("g")
        assert vm._global.is_loaded(N("resource", 1)) and not vm._local.is_loaded(N("resource", 1))
        assert tree._glyph(N("resource", 1), Style()).plain == "[I|G]"

        # i toggles the index axis back off, leaving global context alone.
        await pilot.press("i")
        assert not vm._index.is_loaded(N("resource", 1))
        assert tree._glyph(N("resource", 1), Style()).plain == "[G]"

        # The topic rail filters the resource tree: select Math (linked only to Calculus).
        vm.topic_filter.set_selected(7, True)
        await pilot.pause()
        assert len(tree.root.children) == 1
        assert tree.root.children[0].data.label == "Calculus"
        # And the topic tree itself painted its node.
        assert len(topics.root.children) == 1
