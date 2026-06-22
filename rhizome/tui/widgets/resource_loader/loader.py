"""``ResourceLoader`` — root view for the loader panel.

Vertical panel, two halves split by a rule::

    [section header: Resources]
    [status + legend]
    [search bar]              — view-only filter, drives ``ResourceLoaderModel.set_search``
    [resource tree]           — the [IGL] load-state tree (1fr)
    ──────────────────────────
    [section header: Topics]
    [topic tree]              — the multi-select resource filter (1fr)
    [key hint]

Three regions take focus — the search bar, resource tree, and topic tree — wired into a single
vertical ``FOCUS_GRAPH`` walked by alt+up / alt+down; a vertical-edge step bubbles (``SkipAction``) so
a host can give the keystroke its own meaning. The resource tree binds to the loader VM; the topic
tree binds to the loader's ``topic_filter`` child, whose selection narrows the resources above.
"""

from __future__ import annotations

from rich.text import Text

from textual.actions import SkipAction
from textual.widgets import Rule, Static

from rhizome.app.resource_loader import ResourceLoaderModel
from rhizome.tui.keybindings import Keybind
from rhizome.tui.widgets.resource_loader.loader_tree import ResourceLoaderTree
from rhizome.tui.widgets.resource_loader.status import ResourceStatus
from rhizome.tui.widgets.resource_loader.topic_tree import TopicTree
from rhizome.tui.widgets.shared.focus_orchestration import FocusGraph, FocusOrchestrationMixin
from rhizome.tui.widgets.shared.navigable_feed_item import NavigableViewBase
from rhizome.tui.widgets.shared.search_bar import SearchBar

_RESOURCES_DESC = "Browse and load resources. i indexes · g / l add global / local context."
_TOPICS_DESC = "Select topics to filter the resources above. No selection shows everything."


class ResourceLoader(NavigableViewBase[ResourceLoaderModel], FocusOrchestrationMixin):
    """Root view for the loader panel. See module docstring."""

    # Both bases want the panel focusable; set it explicitly since the mixin (listed last for CSS
    # aggregation) can be shadowed in MRO — lets an external ``.focus()`` land here and delegate inward.
    can_focus = True

    DEFAULT_CSS = """
    /* Paint the panel the chat area's darker surface so the two read cohesively; the child widgets
       below stay transparent and show through to it. */
    ResourceLoader {
        layout: vertical;
        background: $surface-darken-1;
        width: 1fr;
        height: 1fr;
        padding: 0 1 1 1;
    }
    ResourceLoader .rl-section-header {
        height: auto;
        margin-bottom: 1;
        background: transparent;
    }
    ResourceLoader #rl-status {
        height: auto;
        padding: 0 1;
        margin-bottom: 1;
    }
    ResourceLoader #rl-search {
        margin-bottom: 1;
    }
    /* The two stretchy regions split the panel's leftover height (resource tree above the rule, topic
       tree below). ``:focus`` matches each tree directly — it IS the focus leaf, so the border
       brighten doesn't trigger a descendant cascade. */
    ResourceLoader .resource-box {
        border: solid #3a3a3a;
        border-title-align: left;
        border-title-color: rgb(120,120,120);
        background: transparent;
    }
    ResourceLoader #rl-resource-tree {
        height: 1fr;
    }
    ResourceLoader #rl-resource-tree:focus {
        border: solid #6a6a6a;
    }
    ResourceLoader #rl-divider {
        height: 1;
        color: #3a3a3a;
        margin: 1 0;
    }
    ResourceLoader #rl-topic-tree {
        height: 1fr;
    }
    ResourceLoader #rl-topic-tree:focus {
        border: solid #6a6a6a;
    }
    ResourceLoader #rl-keyhint {
        height: auto;
        margin-top: 1;
        padding: 0 1;
        background: transparent;
    }
    """

    BINDINGS = [
        Keybind.FocusUp.  as_binding("focus_neighbour('up')",   show=False),
        Keybind.FocusDown.as_binding("focus_neighbour('down')", show=False),
    ]

    # Linear vertical chain through the three focusable regions; alt+up/down walk it.
    FOCUS_GRAPH = FocusGraph(
        source="rl-resource-tree",
        edges={
            "rl-search":        {"down": "rl-resource-tree"},
            "rl-resource-tree": {"up": "rl-search", "down": "rl-topic-tree"},
            "rl-topic-tree":    {"up": "rl-resource-tree"},
        },
    )

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self):
        yield Static(self._section_header("Resources", _RESOURCES_DESC), classes="rl-section-header")

        yield ResourceStatus(self._vm, id="rl-status")
        yield SearchBar[ResourceLoaderModel](self._vm, id="rl-search")

        tree = ResourceLoaderTree(self._vm, id="rl-resource-tree", classes="resource-box")
        tree.border_title = "Resource Tree"
        yield tree

        yield Rule(id="rl-divider")

        yield Static(self._section_header("Topics", _TOPICS_DESC), classes="rl-section-header")

        topic_tree = TopicTree(self._vm.topic_filter, id="rl-topic-tree", classes="resource-box")
        topic_tree.border_title = "Topic Filter"
        yield topic_tree

        yield Static(self._render_keyhint(), id="rl-keyhint")

    async def on_mount(self) -> None:
        # Children are mounted (and subscribed to their VMs) by the time this runs, so the trees will
        # paint when ``load`` resolves. Then land focus on the resource tree.
        await self._vm.load()
        self.focus_first()

    # ------------------------------------------------------------------
    # Focus orchestration
    # ------------------------------------------------------------------

    def action_focus_neighbour(self, direction: str) -> None:
        # None means the step had no in-graph target (a vertical edge); bubble it so a host can give
        # alt+up/down its own meaning there.
        if self.focus_neighbour(direction) is None:  # type: ignore[arg-type]
            raise SkipAction()

    # ------------------------------------------------------------------
    # Static content
    # ------------------------------------------------------------------

    def _section_header(self, title: str, description: str) -> Text:
        text = Text()
        text.append(title + "\n", style="bold")
        text.append(description, style="#707070")
        return text

    _KEYHINT_PAIRS = (
        ("alt+↑↓", "navigate"),
        ("i", "index"),
        ("g / l", "global / local"),
        ("←→", "expand/collapse"),
    )

    def _render_keyhint(self) -> Text:
        text = Text()
        for i, (key, action) in enumerate(self._KEYHINT_PAIRS):
            if i:
                text.append("   ")
            text.append(key, style="#a0a0a0")
            text.append(" ")
            text.append(action, style="#707070")
        return text
