"""MergeTree widget layout: label fitting + the spacing math it rests on.

The widget reserves horizontal room so a node's label never collides with a same-rank neighbour, and
grows the canvas so labels are not clipped at the right edge. These pin that behaviour on the pure
``build_layout`` / ``render`` seams (no terminal), and guard that short / absent labels still lay out at
the plain ``COL_GAP`` spacing — i.e. the change is a no-op for everything that pre-dates label fitting.
"""

from textual.app import App, ComposeResult
from textual.containers import ScrollableContainer

from rhizome.tui.widgets.shared.merge_tree import (
    COL_GAP, GraphNode, MergeTree, _gap_after, _isotonic, build_layout, render,
)


def _plain(nodes) -> str:
    return render(build_layout(nodes), nodes).to_plain()


# ------------------------------------------------------------------
# Spacing math
# ------------------------------------------------------------------

def test_gap_after_floors_at_col_gap_for_short_labels():
    # A 0- or 1-cell label needs no more than the plain minimum, so old layouts are untouched; a wider
    # label pushes the next marker out one column per extra cell.
    assert _gap_after("n", {}) == COL_GAP
    assert _gap_after("n", {"n": 1}) == COL_GAP
    assert _gap_after("n", {"n": 2}) == 5
    assert _gap_after("n", {"n": 12}) == 15


def test_isotonic_uniform_gaps_match_even_spacing():
    # Equal gaps reduce to the original even-spacing case, centred on the mean (here 0).
    assert _isotonic([0.0, 0.0, 0.0], [4.0, 4.0]) == [-4.0, 0.0, 4.0]


def test_isotonic_variable_gap_separates_to_the_demanded_width():
    # A single wide gap pushes the pair apart by exactly that much, still centred.
    assert _isotonic([0.0, 0.0], [10.0]) == [-5.0, 5.0]


def test_isotonic_leaves_already_spaced_points_untouched():
    assert _isotonic([0.0, 10.0], [4.0]) == [0.0, 10.0]


# ------------------------------------------------------------------
# Label fitting (render)
# ------------------------------------------------------------------

def test_long_labels_in_a_chain_render_in_full():
    # One node per rank: each label has the whole right side free, so none is clipped by the canvas.
    plain = _plain([
        GraphNode("r", (), "main"),
        GraphNode("a", ("r",), "refactor-auth"),
        GraphNode("b", ("a",), "fix-tests"),
    ])
    for label in ("main", "refactor-auth", "fix-tests"):
        assert label in plain


def test_sibling_labels_do_not_collide():
    # Three named children of one fork: every label survives intact, none overrunning the next marker.
    nodes = [
        GraphNode("r", (), "main"),
        GraphNode("a", ("r",), "experiment-a"),
        GraphNode("b", ("r",), "experiment-b"),
        GraphNode("c", ("r",), "hotfix"),
    ]
    plain = _plain(nodes)
    for label in ("experiment-a", "experiment-b", "hotfix"):
        assert label in plain

    # The widened columns hold each left sibling's label clear of its right neighbour's marker.
    layout = build_layout(nodes)
    sib = sorted(("a", "b", "c"), key=lambda n: layout.col[n])
    for left, right in zip(sib, sib[1:]):
        assert layout.col[right] - layout.col[left] >= _gap_after(left, {left: 12})


def test_single_char_labels_keep_col_gap_spacing():
    # The pre-label baseline: 1-cell labels must not widen anything — siblings stay exactly COL_GAP apart.
    nodes = [
        GraphNode("r", (), "R"),
        GraphNode("a", ("r",), "a"),
        GraphNode("b", ("r",), "b"),
    ]
    layout = build_layout(nodes)
    assert layout.col["b"] - layout.col["a"] == COL_GAP


# ------------------------------------------------------------------
# Cursor-follow scrolling
# ------------------------------------------------------------------

class _ScrollApp(App):
    """A minimal owner: it provides the scroll container and reveals the cursor cell the widget publishes —
    the integration every consumer wires (the widget itself neither scrolls nor knows who does)."""

    CSS = "#scroll { width: 24; height: 10; overflow: auto auto; align: center middle; }"

    def __init__(self, nodes) -> None:
        super().__init__()
        self._graph = nodes

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="scroll"):
            yield MergeTree(self._graph, id="tree")

    def on_mount(self) -> None:
        self.query_one(MergeTree).focus()

    def on_merge_tree_cursor_moved(self, event: MergeTree.CursorMoved) -> None:
        if event.region is None:
            return
        tree = self.query_one(MergeTree)
        target = event.region.translate(tree.virtual_region.offset)
        self.query_one("#scroll").scroll_to_region(target, animate=False)


async def test_cursor_follows_into_view_when_the_diagram_overflows():
    # A fork far wider than its viewport: walking the cursor across the siblings must scroll the container so
    # the tip stays visible, then unscroll on the way back. The widget publishes the cursor cell; the owner
    # (here _ScrollApp) reveals it.
    nodes = [GraphNode("R", (), "main")] + [
        GraphNode(f"c{i}", ("R",), f"experiment-branch-{i}") for i in range(8)
    ]
    async with _ScrollApp(nodes).run_test(size=(40, 16)) as pilot:
        scroll = pilot.app.query_one("#scroll")
        assert scroll.max_scroll_x > 0                 # the tree really does overflow horizontally

        await pilot.press("down")                      # onto the leftmost child
        await pilot.pause()
        left = scroll.scroll_offset.x
        for _ in range(7):                             # walk right to the last (off-screen) sibling
            await pilot.press("right")
            await pilot.pause()
        assert scroll.scroll_offset.x > left           # scrolled right to follow the cursor

        for _ in range(7):                             # …and back to the first
            await pilot.press("left")
            await pilot.pause()
        assert scroll.scroll_offset.x <= left          # returned toward the origin
