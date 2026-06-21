"""Interactive demo: build a `MergeTree` *live* and watch the shared widget redraw it.

Where the sibling ``merge_tree`` example cycles through a fixed gallery, this one starts from a lone root
and lets you grow the graph by hand — every keypress mutates a real `MergeTree` data structure, which we
re-adapt to the widget's `GraphNode` descriptors and push back in. So it doubles as a demo of the data
structure's mutation API (``add_edge`` / ``remove`` / ``prune``) and its invariants (single root, always
acyclic) — the widget just paints whatever falls out.

Controls
--------
    b           branch here   — add a child to the cursor node, cursor stays   (build width / forks)
    d           deepen here   — add a child and descend into it                (build depth / chains)
    x           delete here   — remove the cursor node's subtree (root is safe)
    m           merge mode    — then enter on one node, enter on another: a new ◆ child of both
    ↑↓←→        move the cursor   ·   esc cancel merge   ·   q quit

Run it:

    uv run python -m rhizome.tui.widgets.shared.examples.merge_tree_builder
"""

from __future__ import annotations

from typing import Hashable, Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer
from textual.widgets import Static

from rhizome.utils.data_structures.merge_tree import MergeTree
from rhizome.tui.widgets.shared.merge_tree import (
    GraphNode,
    MergeTree as MergeTreeView,   # the widget; aliased so it doesn't shadow the data structure above
)


def to_nodes(tree: MergeTree, mark: Optional[Hashable] = None) -> list[GraphNode]:
    """Adapt the `MergeTree` to the widget's flat descriptor list, flagging `mark` as a merge candidate.

    The flagged node gets a distinct ◈ glyph so a pending first-parent stays visible even when the cursor
    (whose own colour wins) happens to sit on it — exactly the per-node ``marker`` / ``style`` hook the
    widget exposes for domain presentation.
    """
    nodes = []
    for n in tree.graph:
        if n == mark:
            nodes.append(GraphNode(n, tree.parents(n), str(n), marker="◈", style="bold yellow"))
        else:
            nodes.append(GraphNode(n, tree.parents(n), str(n)))
    return nodes


# ======================================================================================================
# TEXTUAL APP
# ======================================================================================================

class MergeTreeBuilder(App):
    """Grow a `MergeTree` interactively; the widget owns the cursor, this app owns the model + the edits."""

    CSS = """
    Screen { background: $surface-darken-2; }
    #header { dock: top; height: 1; background: $primary; color: $text; text-style: bold; padding: 0 1; }
    #hint   { dock: bottom; height: 1; background: $surface; padding: 0 1; }
    #scroll { width: 1fr; height: 1fr; overflow: auto auto; align-horizontal: center; }
    #canvas { padding: 1 2; }
    """

    BINDINGS = [
        Binding("b", "branch", "Branch"),
        Binding("d", "deepen", "Deepen"),
        Binding("x", "delete", "Delete"),
        Binding("m", "merge_mode", "Merge"),
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._tree: MergeTree = MergeTree("R")
        self._counter = 0                       # hands out fresh node ids: "1", "2", "3", …
        self._merging = False                   # in merge mode (collecting two parents)?
        self._merge_a: Optional[Hashable] = None  # the first parent picked, once chosen

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        with ScrollableContainer(id="scroll"):
            yield MergeTreeView(id="canvas")
        yield Static(id="hint")

    def on_mount(self) -> None:
        self._refresh()
        self._view.focus()                      # so its cursor keys are live
        self._refresh_bars()

    # -- model <-> widget ----------------------------------------------------

    @property
    def _view(self) -> MergeTreeView:
        return self.query_one("#canvas", MergeTreeView)

    def _fresh_id(self) -> str:
        self._counter += 1
        return str(self._counter)

    def _refresh(self) -> None:
        """Re-adapt the model and push it to the widget (which recovers its cursor by id)."""
        self._view.set_graph(to_nodes(self._tree, self._merge_a if self._merging else None))

    def _refresh_bars(self) -> None:
        n = sum(1 for _ in self._tree.graph)
        trail = " → ".join(str(x) for x in self._view.cursor)
        self.query_one("#header", Static).update(f" Live MergeTree builder    {n} nodes    [dim]@[/] {trail}")
        self.query_one("#hint", Static).update(self._hint_text())

    def _hint_text(self) -> str:
        if self._merging and self._merge_a is None:
            return "[b yellow]MERGE[/] · move to a node · [b]enter[/] picks the first parent · [b]esc[/] cancel"
        if self._merging:
            return (f"[b yellow]MERGE from {self._merge_a}[/] · [b]enter[/] on the second parent "
                    f"→ new ◆ child · [b]esc[/] cancel")
        return "[b]b[/] branch   [b]d[/] deepen   [b]x[/] delete   [b]m[/] merge   ↑↓←→ move   [b]q[/] quit"

    # -- edits ---------------------------------------------------------------

    def action_branch(self) -> None:
        if self._guard_merge():
            return
        tip = self._view.cursor[-1]
        self._tree.add_edge(tip, self._fresh_id())
        self._refresh()                         # cursor path is untouched, so it stays put on `tip`
        self._refresh_bars()

    def action_deepen(self) -> None:
        if self._guard_merge():
            return
        cursor = self._view.cursor
        child = self._fresh_id()
        self._tree.add_edge(cursor[-1], child)
        self._refresh()
        self._view.set_cursor(cursor + (child,))   # follow the new edge down
        self._refresh_bars()

    def action_delete(self) -> None:
        if self._guard_merge():
            return
        cursor = self._view.cursor
        if len(cursor) <= 1:
            self.notify("the root can't be deleted", severity="warning")
            return
        self._tree.remove(cursor[-1])           # drop the node + its incident edges…
        self._tree.prune()                      # …then sweep up whatever that orphaned
        self._refresh()                         # stale cursor path → widget recovers it back to the root
        self._refresh_bars()

    # -- merge mode ----------------------------------------------------------

    def action_merge_mode(self) -> None:
        self._merging = not self._merging       # toggle: a second `m` backs out
        self._merge_a = None
        self._refresh()
        self._refresh_bars()

    def action_cancel(self) -> None:
        if not self._merging:
            return
        self._merging = False
        self._merge_a = None
        self._refresh()
        self._refresh_bars()

    def on_merge_tree_node_highlighted(self, event: MergeTreeView.NodeHighlighted) -> None:
        self._refresh_bars()                    # cursor already moved; just redraw the trail

    def on_merge_tree_node_selected(self, event: MergeTreeView.NodeSelected) -> None:
        if not self._merging:
            return
        if self._merge_a is None:               # first pick: remember it and mark it ◈
            self._merge_a = event.node
            self._refresh()
            self._refresh_bars()
        elif event.node == self._merge_a:
            self.notify("pick a different node for the second parent", severity="warning")
        else:
            self._merge(self._merge_a, event.node)

    def _merge(self, parent_a: Hashable, parent_b: Hashable) -> None:
        """Spawn a fresh ◆ node parented by both picks — always cycle-safe, since the child is brand new."""
        child = self._fresh_id()
        self._tree.add_edge(parent_a, child)
        self._tree.add_edge(parent_b, child)
        self._merging = False
        self._merge_a = None
        self._refresh()
        self._view.set_cursor(next(self._tree.paths_to(child)).nodes())   # park the cursor on the merge
        self._refresh_bars()

    def _guard_merge(self) -> bool:
        if self._merging:
            self.notify("finish the merge or press esc first", severity="warning")
        return self._merging


if __name__ == "__main__":
    MergeTreeBuilder().run()
