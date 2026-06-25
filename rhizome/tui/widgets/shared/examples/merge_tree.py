"""Demo app for the shared `MergeTree` widget — cycle example merge trees with ctrl+left/right.

This is a thin consumer: the layout + rendering live in `rhizome.tui.widgets.shared.merge_tree`. Here we
just build a few `MergeTree` *data structures*, adapt them to the widget's `GraphNode` descriptors, and feed
them in (exactly what a real view-model-owning consumer would do, minus the view-model).

Run it two ways:

    uv run python -m rhizome.tui.widgets.shared.examples.merge_tree dump   # plain-text dump, no TUI
    uv run python -m rhizome.tui.widgets.shared.examples.merge_tree        # interactive: ctrl+←/→ cycles
"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer
from textual.widgets import Static

from rhizome.utils.data_structures.merge_tree import MergeTree
from rhizome.tui.widgets.shared.merge_tree import (
    GraphNode,
    MergeTree as MergeTreeView,   # the widget; aliased so it doesn't shadow the data structure above
    build_layout,
    render,
)


def to_nodes(tree: MergeTree) -> list[GraphNode]:
    """Adapt a `MergeTree` data structure into the widget's flat descriptor list."""
    return [GraphNode(id=n, parents=tree.parents(n), label=str(n)) for n in tree.graph]


def examples() -> list[tuple[str, MergeTree]]:
    """A handful of merge trees that exercise the layout from easy to nasty."""
    def build(edges: list[tuple[str, str]]) -> MergeTree:
        tree = MergeTree("R")
        for parent, child in edges:
            tree.add_edge(parent, child)
        return tree

    return [
        ("Plain tree — no merges (baseline)", build([
            ("R", "a"), ("R", "b"), ("a", "c"), ("a", "d"), ("b", "e"), ("c", "f"), ("d", "g"),
        ])),
        ("Diamond — M merges A and B", build([
            ("R", "A"), ("R", "B"), ("R", "C"), ("A", "M"), ("B", "M"), ("M", "N"), ("C", "K"),
        ])),
        ("Distant merge — long edge E→M past 3 ranks", build([
            ("R", "A"), ("A", "B"), ("B", "C"), ("C", "D"), ("R", "E"), ("D", "M"), ("E", "M"), ("M", "F"),
        ])),
        ("Stacked merges — P←A,B then Q←C,P", build([
            ("R", "A"), ("R", "B"), ("R", "C"), ("A", "P"), ("B", "P"), ("C", "Q"), ("P", "Q"), ("Q", "Z"),
        ])),
        ("N-way merge — M merges A..E (in-degree 5)", build([
            ("R", "A"), ("R", "B"), ("R", "C"), ("R", "D"), ("R", "E"),
            ("A", "M"), ("B", "M"), ("C", "M"), ("D", "M"), ("E", "M"), ("M", "Z"),
        ])),
        ("Mixed-depth 3-way — M merges C@3, D@1, E@1", build([
            ("R", "A"), ("A", "B"), ("B", "C"), ("R", "D"), ("R", "E"),
            ("C", "M"), ("D", "M"), ("E", "M"), ("M", "F"),
        ])),
        ("Non-planar graph", build([
            ("R", "A"), ("R", "B"), ("R", "C"),
            ("A", "D"), ("B", "D"), ("C", "E"),
            ("D", "F"), ("D", "G"),
            ("F", "H"), ("C", "H"),
            ("G", "I"), ("E", "I"),
        ])),
        ("Non-planar — 3 overlapping merges, 3 tracks + bridges", build([
            ("R", "a"), ("R", "b"), ("R", "c"), ("R", "d"), ("R", "e"), ("R", "f"),
            ("a", "X"), ("d", "X"), ("b", "Y"), ("e", "Y"), ("c", "Z"), ("f", "Z"),
        ])),
    ]


# ======================================================================================================
# TEXTUAL APP
# ======================================================================================================

class MergeTreeDemo(App):
    """Cycle through the example merge trees with ctrl+left / ctrl+right."""

    CSS = """
    Screen { background: $surface-darken-2; }
    #header { dock: top; height: 1; background: $primary; color: $text; text-style: bold; padding: 0 1; }
    #hint   { dock: bottom; height: 1; background: $surface; padding: 0 1; }
    #scroll { width: 1fr; height: 1fr; overflow: auto auto; align-horizontal: center; padding: 1 2; }
    """

    BINDINGS = [
        Binding("ctrl+right", "next_tree", "Next"),
        Binding("ctrl+left", "prev_tree", "Prev"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._examples = examples()
        self._idx = 0

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        with ScrollableContainer(id="scroll"):
            yield MergeTreeView(id="canvas")
        yield Static(id="hint")

    def on_mount(self) -> None:
        self._refresh_view()
        self.query_one("#canvas", MergeTreeView).focus()        # so its cursor keys are live

    def _refresh_view(self) -> None:
        title, tree = self._examples[self._idx]
        self.query_one("#header", Static).update(f" {self._idx + 1}/{len(self._examples)}   {title}")
        self.query_one("#canvas", MergeTreeView).set_graph(to_nodes(tree))
        self._show_hint()

    def _show_hint(self, cursor: str = "") -> None:
        trail = f"[cyan]{cursor}[/]    " if cursor else ""
        self.query_one("#hint", Static).update(
            f"[dim]{trail}↑↓←→ move cursor   enter select   ctrl+←/→ cycle examples   q quit[/]"
        )

    def on_merge_tree_node_highlighted(self, event: MergeTreeView.NodeHighlighted) -> None:
        self._show_hint(" → ".join(str(n) for n in event.path))

    def on_merge_tree_cursor_moved(self, event: MergeTreeView.CursorMoved) -> None:
        # This app owns the scroll container, so it reveals the cursor cell the widget publishes.
        if event.region is None:
            return
        canvas = self.query_one("#canvas", MergeTreeView)
        self.query_one("#scroll").scroll_to_region(
            event.region.translate(canvas.virtual_region.offset), animate=False
        )

    def action_next_tree(self) -> None:
        self._idx = (self._idx + 1) % len(self._examples)
        self._refresh_view()

    def action_prev_tree(self) -> None:
        self._idx = (self._idx - 1) % len(self._examples)
        self._refresh_view()


def _dump() -> None:
    """Render every example to plain text — lets us eyeball the layout without a terminal UI."""
    for title, tree in examples():
        nodes = to_nodes(tree)
        print("=" * 78)
        print(title)
        print("=" * 78)
        print(render(build_layout(nodes), nodes).to_plain())
        print()


if __name__ == "__main__":
    import sys

    if "dump" in sys.argv[1:]:
        _dump()
    else:
        MergeTreeDemo().run()
