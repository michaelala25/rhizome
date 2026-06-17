"""A widget that renders a rooted "tree with merges" (any rooted DAG) as a Unicode node-link diagram.

This is a *pure* widget in the Textual sense — like `Tree`, it takes data and draws it, and knows nothing
about view-models. You hand it a flat list of :class:`GraphNode` values (each node plus the ids of its
parents) and it lays the graph out and paints it. A node with two or more parents is drawn as a genuine
convergence: the branches close back together. A consumer that has a view-model owns one of these widgets
and pushes fresh graph nodes into :meth:`MergeTree.set_graph` whenever its model changes — synchronisation
lives in that consumer, never here.

(The widget shares its name with the `MergeTree` *data structure* in ``utils/data_structures``; the module
each lives in disambiguates them, the way Textual's `Tree` widget coexists with the idea of a tree.)

Layout is a small "layered" (Sugiyama-style) pipeline. Each stage is separate so the hard part stays
testable on its own — call :func:`build_layout` / :func:`render` directly to draw to text with no terminal:

  1. RANK   — every node's row is its longest distance from the root. Acyclic + rooted makes this exact in
              one topological pass and guarantees a merge child lands below all its parents.
  2. HOME-X — a first column for every node from a plain tree layout of the "home-parent" forest (each node
              owned by its first parent). Cheap, overlap-free, already tree-shaped.
  3. VIRTUALS — an edge spanning >1 rank is split with invisible waypoints, one per crossed rank, so every
              edge connects adjacent ranks and long edges route as a tidy chain.
  4. RELAX  — barycenter sweeps nudge each node toward the average column of its neighbours, with a
              mean-preserving min-gap (isotonic) step so nothing drifts; then DECLASH slides a merge off any
              column it shares with an unconnected neighbour (which would draw a phantom vertical).
  5. ROUTE  — paint edges into a character grid. Each gap assigns edges to horizontal *tracks* so stacked
              merges stay legible; box glyphs merge bitwise into ┬/┴/┼, and true crossings become double-
              line bridges ╪/╫.

Presentation is per-node (``marker`` / ``style`` on the `GraphNode`), so the widget stays domain-agnostic.
Cursor / keyboard navigation is intentionally not implemented yet — the widget is focusable so it can grow
one without an API change.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Hashable, Sequence

from rich.text import Text
from textual.binding import Binding
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Static

# Layout knobs.
COL_GAP = 4   # minimum columns between two nodes sharing a rank
V_GAP = 2     # blank grid rows between consecutive ranks (so STRIDE rows per rank)
STRIDE = V_GAP + 1


@dataclass(frozen=True)
class GraphNode:
    """One node of the input graph: its id, the ids of its parents, and how to draw it.

    ``parents`` is in arrival order (the first parent is the node's "home" lineage); an empty tuple marks the
    root. ``marker`` / ``style`` default to ``●`` (``◆`` for a merge) in the node's default colour.
    """

    id: Hashable
    parents: tuple[Hashable, ...] = ()
    label: str = ""
    marker: str | None = None
    style: str | None = None


# ======================================================================================================
# CHARACTER GRID  ·  a 2D buffer of glyphs + styles, flattened to text at the end
# ======================================================================================================

# Box-drawing as direction bits: a glyph is the set of sides it connects (Up/Down/Left/Right). Edge
# strokes accumulate as bitmasks (lossless — a "go right" stroke stays distinct from a full "─") and
# only collapse to a glyph at flatten time, so strokes meeting in a cell compose into ┬ / ┴ / ┼ correctly.
U, D, L, R = 1, 2, 4, 8

# Indexed by the OR of those bits: e.g. 10 = D|R = ┌, 14 = D|L|R = ┬, 15 = U|D|L|R = ┼. A lone up/down
# bit renders as │ and a lone left/right as ─ — the direction survives, the glyph just rounds to a line.
_GLYPH = {
    0: " ", 1: "│", 2: "│", 3: "│", 4: "─", 5: "┘", 6: "┐", 7: "┤",
    8: "─", 9: "└", 10: "┌", 11: "├", 12: "─", 13: "┴", 14: "┬", 15: "┼",
}

# A *crossing* — one edge's straight run passing over another's — is drawn as a double-line "bridge", the
# line drawn second riding on top: ╪ = horizontal over vertical, ╫ = vertical over horizontal. Two high
# bits record that a through-run has already passed a cell, so the second arrival knows it is a crossing.
PASS_V, PASS_H = 16, 32
HL = 64                       # this cell lies on the highlighted cursor path

# Cursor colours: the whole root→current path lights up in PATH_STYLE, its tip (the current node) in the
# stronger CURSOR_STYLE; everything else stays dim / in the node's own style.
PATH_STYLE = "cyan"
CURSOR_STYLE = "bold black on cyan"


class Grid:
    """A character canvas. Line strokes live in a bitmask plane; nodes/labels are character overrides
    that win at flatten time. Paint in any order, collapse to one `Text`/string at the end."""

    def __init__(self, w: int, h: int) -> None:
        self.w, self.h = w, h
        self.bits: list[list[int]] = [[0] * w for _ in range(h)]
        self.over: list[list[tuple[str, str | None] | None]] = [[None] * w for _ in range(h)]
        self.bridge: list[list[str | None]] = [[None] * w for _ in range(h)]   # 'H'→╪ / 'V'→╫ at crossings

    def put(self, r: int, c: int, ch: str, style: str | None = None) -> None:
        """Stamp a character (node / glyph) that overrides any line strokes beneath it."""
        if 0 <= r < self.h and 0 <= c < self.w:
            self.over[r][c] = (ch, style)

    def stroke(self, r: int, c: int, bits: int, hl: bool = False) -> None:
        """Accumulate edge-line direction bits in a cell (used for turns / single-direction arms)."""
        if 0 <= r < self.h and 0 <= c < self.w:
            self.bits[r][c] |= bits | (HL if hl else 0)

    def cross_v(self, r: int, c: int, hl: bool = False) -> None:
        """A vertical through-run. If a horizontal run already passed here, this one bridges over it (╫)."""
        if 0 <= r < self.h and 0 <= c < self.w:
            if self.bits[r][c] & PASS_H and not self.bits[r][c] & PASS_V:
                self.bridge[r][c] = "V"
            self.bits[r][c] |= U | D | PASS_V | (HL if hl else 0)

    def cross_h(self, r: int, c: int, hl: bool = False) -> None:
        """A horizontal through-run. If a vertical run already passed here, this one bridges over it (╪)."""
        if 0 <= r < self.h and 0 <= c < self.w:
            if self.bits[r][c] & PASS_V and not self.bits[r][c] & PASS_H:
                self.bridge[r][c] = "H"
            self.bits[r][c] |= L | R | PASS_H | (HL if hl else 0)

    def label(self, r: int, c: int, text: str, style: str | None = None) -> None:
        """Best-effort text: write into truly blank cells only, stop at the first occupied one."""
        for i, ch in enumerate(text):
            cc = c + i
            if not (0 <= r < self.h and 0 <= cc < self.w):
                break
            if self.over[r][cc] is not None or self.bits[r][cc] != 0:
                break
            self.over[r][cc] = (ch, style)

    def _cell(self, r: int, c: int) -> tuple[str, str | None]:
        if self.over[r][c] is not None:
            return self.over[r][c]
        b = self.bits[r][c]
        style = PATH_STYLE if b & HL else "dim"        # a stroke on the cursor path lights up
        if self.bridge[r][c]:
            return ("╪" if self.bridge[r][c] == "H" else "╫", style)
        glyph = b & 0b1111                             # mask off PASS_* / HL tracking bits for the lookup
        return (_GLYPH[glyph], style) if glyph else (" ", None)

    def to_plain(self) -> str:
        return "\n".join(
            "".join(self._cell(r, c)[0] for c in range(self.w)).rstrip() for r in range(self.h)
        )

    def to_text(self) -> Text:
        out = Text()
        for r in range(self.h):
            for c in range(self.w):
                ch, style = self._cell(r, c)
                out.append(ch, style=style or "")
            if r < self.h - 1:
                out.append("\n")
        return out


# ======================================================================================================
# LAYOUT
# ======================================================================================================

@dataclass(frozen=True)
class _Virtual:
    """An invisible waypoint splitting a long edge so it spans only one rank at a time."""

    edge: tuple        # the (src, dst) real edge this waypoint belongs to
    rank: int


@dataclass
class Layout:
    rank: dict[Hashable, int]            # every node (real + virtual) -> rank (logical layer)
    col: dict[Hashable, int]             # every node -> grid column
    edges: list[tuple[Hashable, Hashable]]   # adjacent-rank edges of the "proper" graph
    virtuals: list[_Virtual]
    is_merge: set[Hashable]              # real nodes with >= 2 parents
    width: int
    height: int
    rank_row: dict[int, int]             # rank -> grid row (variable: a busy gap is taller than STRIDE)
    edge_jog: dict[tuple, int]           # edge -> the grid row its horizontal segment (its track) sits on

    def row(self, node: Hashable) -> int:
        return self.rank_row[self.rank[node]]


def _adjacency(nodes: Sequence[GraphNode]) -> tuple[list, dict, dict, list, Hashable]:
    """Derive (node order, parents, children, edges, root) from the flat `GraphNode` list.

    Children are ordered by their position in ``nodes`` (its natural creation order); the single parentless
    node is the root. The graph is assumed complete (every referenced parent is itself a node) and acyclic.
    """
    order = [n.id for n in nodes]
    parents = {n.id: tuple(n.parents) for n in nodes}
    children: dict[Hashable, list] = {nid: [] for nid in order}
    for nid in order:
        for p in parents[nid]:
            children[p].append(nid)
    edges = [(p, nid) for nid in order for p in parents[nid]]
    root = next(nid for nid in order if not parents[nid])
    return order, parents, children, edges, root


def _topo_order(order: list, parents: dict, children: dict) -> list:
    """Kahn's algorithm — every node after all its parents (deterministic: ties keep input order)."""
    indeg = {nid: len(parents[nid]) for nid in order}
    queue = deque(nid for nid in order if indeg[nid] == 0)
    out: list = []
    while queue:
        nid = queue.popleft()
        out.append(nid)
        for c in children[nid]:
            indeg[c] -= 1
            if indeg[c] == 0:
                queue.append(c)
    return out


def _ranks(topo: list, parents: dict) -> dict[Hashable, int]:
    """Longest-path layering: rank = 1 + the deepest parent (root = 0)."""
    rank: dict[Hashable, int] = {}
    for n in topo:                                  # parents always processed before children
        ps = parents[n]
        # Longest path (max, not min): a merge child must clear its DEEPEST parent, which keeps every edge
        # pointing strictly downward (rank strictly increases from parent to child).
        rank[n] = 0 if not ps else 1 + max(rank[p] for p in ps)
    return rank


def _home_x(root: Hashable, children: dict, parents: dict, order: list) -> dict[Hashable, float]:
    """Starting columns from a plain tree layout of the home-parent forest.

    A node's *home* parent is its first parent. Following home parents upward always reaches the root, so the
    home edges form a spanning tree — we pack it like an ordinary tree diagram (disjoint bands, parent
    centred over its children).
    """
    def home_parent(nid: Hashable) -> Hashable | None:
        ps = parents[nid]
        return ps[0] if ps else None

    # Keep only the edge from each node's FIRST parent. One such choice per node carves a single spanning
    # tree out of the DAG — every non-root node is owned by exactly one parent, leaving no merges to confuse
    # the ordinary tree layout below.
    home_children: dict[Hashable, list] = {nid: [] for nid in order}
    for nid in order:
        for c in children[nid]:
            if home_parent(c) == nid:
                home_children[nid].append(c)

    width: dict[Hashable, float] = {}

    # Subtree width in columns: a leaf claims COL_GAP, a parent the sum of its children. Disjoint bands, so
    # nothing in the home tree can overlap horizontally.
    def measure(nid: Hashable) -> float:
        kids = home_children[nid]
        width[nid] = sum(measure(k) for k in kids) if kids else float(COL_GAP)
        return width[nid]

    measure(root)

    x: dict[Hashable, float] = {}

    # Walk each subtree's band left→right, dropping every child into its own slice, then centre the parent
    # over the children it produced.
    def place(nid: Hashable, left: float) -> None:
        kids = home_children[nid]
        if not kids:
            x[nid] = left + width[nid] / 2
            return
        cursor = left
        for k in kids:
            place(k, cursor)
            cursor += width[k]
        x[nid] = (x[kids[0]] + x[kids[-1]]) / 2     # centre over home-children

    place(root, 0.0)
    return x


def _proper(edges: list, rank: dict) -> tuple[list[_Virtual], list[tuple]]:
    """Split every multi-rank edge into a chain through virtual waypoints (one per crossed rank)."""
    virtuals: list[_Virtual] = []
    out_edges: list[tuple] = []
    for p, c in edges:
        if rank[c] - rank[p] == 1:
            out_edges.append((p, c))                    # already one rank tall — keep as is
            continue
        # Spans multiple ranks: thread one waypoint through each intermediate rank so the emitted chain
        # (p → v → … → c) is made entirely of one-rank-tall edges, which routing can handle uniformly.
        prev: Hashable = p
        for r in range(rank[p] + 1, rank[c]):
            v = _Virtual((p, c), r)
            virtuals.append(v)
            out_edges.append((prev, v))
            prev = v
        out_edges.append((prev, c))
    return virtuals, out_edges


def _isotonic(desired: list[float], gap: float) -> list[float]:
    """Closest positions to `desired` (in order) keeping each at least `gap` past the previous.

    Substituting q_i = desired_i - i*gap turns the gap constraint into "q must be non-decreasing", the
    classic isotonic-regression / pool-adjacent-violators problem. The L2 fit pools tied runs at their mean,
    so the centroid is preserved — no sideways drift, unlike a naive left-to-right push.
    """
    blocks: list[list[float]] = []        # each: [count, sum]; a block's shared position = sum/count
    for i, d in enumerate(desired):
        # Append this point as its own block, then while the previous block sits at or above it (the
        # non-decreasing order is violated) merge the two into one block at their combined mean. Merging at
        # the mean is what preserves the centre of mass — a squeezed cluster stays centred.
        blocks.append([1.0, d - i * gap])
        while len(blocks) >= 2 and blocks[-2][1] / blocks[-2][0] >= blocks[-1][1] / blocks[-1][0]:
            c2, s2 = blocks.pop()
            c1, s1 = blocks.pop()
            blocks.append([c1 + c2, s1 + s2])
    out: list[float] = []
    for count, total in blocks:
        out.extend([total / count] * int(count))      # every point in a block shares the block's position
    return [v + i * gap for i, v in enumerate(out)]    # undo the −i*gap shift to restore real spacing


def _relax(x: dict, layers: dict[int, list], parents: dict, children: dict, iters: int = 6) -> None:
    """Barycenter sweeps: alternately pull each node toward its parents' / children's average column,
    re-spacing each rank to keep `COL_GAP`. Order within a rank is fixed, so no crossings are created."""
    # Two complementary passes per iteration. Down alone aligns children under parents but ignores how the
    # children spread; up alone centres parents over children but ignores the parents above. Alternating a
    # few times settles to a layout balanced from both directions. (`else x[n]`: a node with nothing to
    # average against on that side holds still — the root has no parents, a leaf has no children.)
    ordered = sorted(layers)
    for _ in range(iters):
        for r in ordered:                                   # downward: node → mean column of its parents
            nodes = layers[r]
            want = [sum(x[p] for p in parents[n]) / len(parents[n]) if parents[n] else x[n] for n in nodes]
            for n, pos in zip(nodes, _isotonic(want, COL_GAP)):
                x[n] = pos
        for r in reversed(ordered):                         # upward: node → mean column of its children
            nodes = layers[r]
            want = [sum(x[c] for c in children[n]) / len(children[n]) if children[n] else x[n] for n in nodes]
            for n, pos in zip(nodes, _isotonic(want, COL_GAP)):
                x[n] = pos


def _color_gap(gedges: list[tuple], col: dict) -> dict[tuple, int]:
    """Assign every edge crossing one rank-gap a *track* (its index among the rows of that gap), so two
    bundles of edges that would otherwise share a row don't collapse into one indistinguishable line.

    Edges that share an endpoint belong to the same bundle — a parent fanning out to several children, or
    several parents converging on one child, must stay together to draw as a single ┬/┴. Each bundle spans a
    column interval; bundles whose intervals overlap get different tracks (greedy interval colouring,
    leftmost first → fewest tracks). Crossings between tracks then fall out of the bitmask on their own.
    """
    # Union edges that share a parent or a child into bundles (connected components over endpoints).
    uf = {e: e for e in gedges}

    def find(e):
        root = e
        while uf[root] != root:
            root = uf[root]
        while uf[e] != root:                # path-compress so repeat lookups stay cheap
            uf[e], e = root, uf[e]
        return root

    seen_parent: dict = {}
    seen_child: dict = {}
    for e in gedges:
        a, b = e
        if a in seen_parent:
            uf[find(e)] = find(seen_parent[a])
        else:
            seen_parent[a] = e
        if b in seen_child:
            uf[find(e)] = find(seen_child[b])
        else:
            seen_child[b] = e

    bundles: dict = defaultdict(list)
    for e in gedges:
        bundles[find(e)].append(e)

    # Each bundle → the column interval it spans; colour them greedily, leftmost first. A track is free for a
    # bundle iff the last bundle placed there ended (strictly) to its left.
    intervals = [(min(col[x] for e in es for x in e), max(col[x] for e in es for x in e), es)
                 for es in bundles.values()]
    intervals.sort(key=lambda iv: (iv[0], iv[1]))

    track_right: list[int] = []             # right edge of the last bundle on each track
    edge_track: dict[tuple, int] = {}
    for lo, hi, es in intervals:
        t = next((i for i, right in enumerate(track_right) if right < lo), None)
        if t is None:                       # every existing track still overlaps here — open a new one
            t = len(track_right)
            track_right.append(hi)
        else:
            track_right[t] = hi
        for e in es:
            edge_track[e] = t
    return edge_track


def _assign_tracks(rank: dict, col: dict, edges: list[tuple]) -> tuple[dict, dict, int]:
    """Turn logical ranks into actual grid rows, growing a gap only when its edges need extra tracks.
    Returns (rank → grid row, edge → its horizontal jog row, total grid height)."""
    by_gap: dict[int, list] = defaultdict(list)
    for a, b in edges:
        by_gap[rank[a]].append((a, b))      # an edge lives in the gap just below its parent's rank

    edge_track: dict[tuple, int] = {}
    tracks_per_gap: dict[int, int] = {}
    for g, gedges in by_gap.items():
        colouring = _color_gap(gedges, col)
        edge_track.update(colouring)
        tracks_per_gap[g] = max(colouring.values()) + 1

    # Rank rows are a prefix sum of gap heights. A gap is STRIDE tall by default (which already hides V_GAP
    # track rows for free); only a gap needing more tracks than that grows, so simple trees are untouched.
    max_rank = max(rank.values())
    rank_row = {0: 0}
    for g in range(max_rank):
        rank_row[g + 1] = rank_row[g] + max(STRIDE, tracks_per_gap.get(g, 1) + 1)

    edge_jog = {e: rank_row[rank[e[0]]] + 1 + t for e, t in edge_track.items()}
    height = rank_row[max_rank] + 1
    return rank_row, edge_jog, height


def _declash(rank: dict, col: dict, edges: list[tuple], movable: set, iters: int = 6) -> None:
    """Nudge each merge node out of any column it shares with an *unconnected* node one rank above or below.

    A shared column draws a phantom vertical: the merge reads as if it drops straight out of (or into) a node
    it has no edge to. Merge columns come from a barycentre and can move, so we slide them to the nearest
    clear column; tree nodes and waypoints stay put. This is the vertical cousin of track assignment — tracks
    separate overlapping horizontals, this separates phantom verticals.
    """
    connected: set = set()
    for a, b in edges:
        connected.add((a, b))
        connected.add((b, a))
    by_rank: dict[int, list] = defaultdict(list)
    for n in col:
        by_rank[rank[n]].append(n)

    def phantom(n, c) -> bool:                              # column c (for node n) sits under/over a stranger
        return any(col[m] == c and (n, m) not in connected
                   for adj in (rank[n] - 1, rank[n] + 1) for m in by_rank.get(adj, []))

    def clear(n, c) -> bool:                                # c keeps COL_GAP from row-mates and is no phantom
        return (all(abs(col[m] - c) >= COL_GAP for m in by_rank[rank[n]] if m is not n)
                and not phantom(n, c))

    # Process in a stable (rank, column) order — `movable` is a set, so iterating it directly would make the
    # nudges depend on hash ordering and the layout non-reproducible.
    for _ in range(iters):
        moved = False
        for n in sorted(movable, key=lambda k: (rank[k], col[k], str(k))):
            if not phantom(n, col[n]):
                continue
            for delta in range(1, 3 * COL_GAP):             # search outward for the nearest clear column
                cand = next((col[n] + d for d in (delta, -delta) if clear(n, col[n] + d)), None)
                if cand is not None:
                    col[n] = cand
                    moved = True
                    break
        if not moved:                                       # fixed point — nothing left to separate
            break


def build_layout(nodes: Sequence[GraphNode]) -> Layout:
    """Run the whole pipeline: adjacency → rank → home-x → virtuals → relax → declash → grid coordinates."""
    order, parents, children, edges, root = _adjacency(nodes)
    topo = _topo_order(order, parents, children)
    rank = _ranks(topo, parents)                            # stage 1: rows (fixed from here on)
    x = _home_x(root, children, parents, order)             # stage 2: rough columns from the home tree
    virtuals, pedges = _proper(edges, rank)                 # stage 3: long edges → chains of one-rank hops

    # Seed each waypoint proportionally along the straight line between its edge's real endpoints, so a long
    # edge starts out straight and relaxation only has to nudge it.
    for v in virtuals:
        p, c = v.edge
        rank[v] = v.rank
        t = (v.rank - rank[p]) / (rank[c] - rank[p])
        x[v] = x[p] + t * (x[c] - x[p])

    # Adjacency over the proper graph (real nodes + waypoints) — the neighbours relaxation averages against.
    pparents: dict = defaultdict(list)
    pchildren: dict = defaultdict(list)
    for a, b in pedges:
        pchildren[a].append(b)
        pparents[b].append(a)

    # Group into ranks and fix each rank's left→right order ONCE, by initial column. _relax only slides nodes
    # sideways, never reorders them, so this ordering (and whatever crossings it implies) is final.
    all_nodes = list(order) + virtuals
    seq = {n: i for i, n in enumerate(all_nodes)}           # stable tie-break when two share a column
    layers: dict[int, list] = defaultdict(list)
    for n in all_nodes:
        layers[rank[n]].append(n)
    for r in layers:
        layers[r].sort(key=lambda n: (x[n], seq[n]))

    _relax(x, layers, pparents, pchildren)                  # stage 4: settle the columns

    # Snap floats to integer grid columns, shifting so the leftmost node lands at column 2 (a small margin).
    col = {n: int(round(x[n] - min(x.values()))) + 2 for n in all_nodes}

    # Stage 4.5: slide merge columns off any phantom vertical alignment, then re-seat at the left margin.
    is_merge = {nid for nid in order if len(parents[nid]) >= 2}
    _declash(rank, col, pedges, is_merge)
    col = {n: c - (min(col.values()) - 2) for n, c in col.items()}
    width = max(col.values()) + 3

    # Stage 5-prep: give each edge a horizontal track so merges sharing a gap stay legible, and resolve the
    # logical ranks into grid rows (taller wherever a gap carries more than one track).
    rank_row, edge_jog, height = _assign_tracks(rank, col, pedges)
    return Layout(rank, col, pedges, virtuals, is_merge, width, height, rank_row, edge_jog)


# ======================================================================================================
# RENDER  ·  Layout -> Grid
# ======================================================================================================

def _route(grid: Grid, ca: int, ra: int, cb: int, rb: int, corner: int, hl: bool = False) -> None:
    """Draw one adjacent-rank edge: drop from the parent, jog sideways on its assigned track row
    (`corner`), drop into the child. Only direction bits are written, so where this edge meets or crosses
    another, the bitmask composes the right junction (┬/┴) or crossing (┼/bridge) for free at flatten.
    `hl` marks every cell as part of the cursor path so it renders highlighted."""
    for r in range(ra + 1, corner):                         # upper vertical, in the parent's column
        grid.cross_v(r, ca, hl)
    grid.stroke(corner, ca, U, hl)                          # arrive at the track row from above…
    if ca != cb:
        lo, hi = sorted((ca, cb))
        for c in range(lo + 1, hi):                         # the horizontal run between the two columns
            grid.cross_h(corner, c, hl)
        grid.stroke(corner, ca, R if cb > ca else L, hl)    # …turn toward the child → └ or ┘
        grid.stroke(corner, cb, L if cb > ca else R, hl)    # child column: the arm coming from the parent
    grid.stroke(corner, cb, D, hl)                          # …then leave downward → ┌/┐ (or │ if ca == cb)
    for r in range(corner + 1, rb):                         # lower vertical, in the child's column
        grid.cross_v(r, cb, hl)


def _real_edge(a: Hashable, b: Hashable) -> tuple:
    """The real (parent, child) edge a proper edge belongs to — its virtual chain's source→dest, or itself."""
    if isinstance(a, _Virtual):
        return a.edge
    if isinstance(b, _Virtual):
        return b.edge
    return (a, b)


def render(layout: Layout, nodes: Sequence[GraphNode], cursor: tuple = (), show_labels: bool = True) -> Grid:
    """Paint a built `Layout` into a `Grid`. Edges first, then nodes on top — a node cell overrides any
    stroke that routed into it (see `Grid._cell`), so the ● / ◆ always wins its own square.

    `cursor` is the root→current id-path. Only that realization lights up: its nodes and the edges *between
    consecutive path nodes* (not every edge into a merged node), with the tip in `CURSOR_STYLE`.
    """
    grid = Grid(layout.width, layout.height)
    tip = cursor[-1] if cursor else None
    path_nodes = set(cursor)
    path_edges = set(zip(cursor, cursor[1:]))               # consecutive (parent, child) pairs on the path

    for edge in layout.edges:
        a, b = edge
        hl = _real_edge(a, b) in path_edges
        _route(grid, layout.col[a], layout.row(a), layout.col[b], layout.row(b), layout.edge_jog[edge], hl)

    for v in layout.virtuals:                               # a waypoint's own cell bridges its two segments
        grid.cross_v(layout.row(v), layout.col[v], v.edge in path_edges)

    for node in nodes:
        r, c = layout.row(node.id), layout.col[node.id]
        merge = node.id in layout.is_merge
        if node.id == tip:
            style = CURSOR_STYLE
        elif node.id in path_nodes:
            style = PATH_STYLE
        elif node.style is not None:
            style = node.style
        else:
            style = "bold magenta" if merge else None
        grid.put(r, c, node.marker or ("◆" if merge else "●"), style)
        if show_labels and node.label:
            grid.label(r, c + 2, node.label, "dim")

    return grid


# ======================================================================================================
# WIDGET
# ======================================================================================================

class MergeTree(Static, can_focus=True):
    """Renders a rooted DAG-with-merges from a list of :class:`GraphNode` values, with a navigable cursor.

    Pure view: hand it `GraphNode`s via the constructor or :meth:`set_graph` and it lays out and paints. The
    CURSOR is a root→current *path* (a tuple of node ids) — the realization of how you reached the current
    node — so it stays unambiguous through merges: `up` retraces the exact way you came. The path lights up
    in the diagram. Arrows move structurally (up = parent, down = first child, left/right = siblings); `enter`
    selects. It owns the cursor outright (no view-model), so a consumer reacts to :class:`NodeHighlighted` /
    :class:`NodeSelected`, or drives the cursor itself with :meth:`set_cursor`.
    """

    DEFAULT_CSS = """
    MergeTree {
        width: auto;
        height: auto;
    }
    """

    BINDINGS = [
        Binding("up", "cursor_up", "Parent", show=False),
        Binding("down", "cursor_down", "Child", show=False),
        Binding("left", "cursor_left", "Prev sibling", show=False),
        Binding("right", "cursor_right", "Next sibling", show=False),
        Binding("enter", "select", "Select", show=False),
    ]

    cursor: reactive[tuple] = reactive((), init=False)
    """The root→current id-path. View-owned; assign to move it, or use :meth:`set_cursor`."""

    class NodeHighlighted(Message):
        """Posted on a user cursor move — the highlighted root→node path changed."""

        def __init__(self, path: tuple, node: Hashable) -> None:
            super().__init__()
            self.path = path
            self.node = node

    class NodeSelected(Message):
        """Posted when `enter` is pressed on the cursor node."""

        def __init__(self, path: tuple, node: Hashable) -> None:
            super().__init__()
            self.path = path
            self.node = node

    def __init__(self, nodes: Sequence[GraphNode] = (), *, show_labels: bool = True, **kwargs) -> None:
        super().__init__(**kwargs)
        self._show_labels = show_labels
        # NB: not `self._nodes` — that is Textual's child-widget NodeList; shadowing it makes the framework
        # try to walk these graph nodes as if they were mounted widgets.
        self._graph_nodes: list[GraphNode] = list(nodes)
        self._layout: Layout | None = None
        self._children: dict[Hashable, list] = {}     # id -> child ids, for navigation
        self._edges: set = set()                      # (parent, child) pairs, for cursor recovery
        self._root: Hashable | None = None

    # -- public API -----------------------------------------------------------

    def set_graph(self, nodes: Sequence[GraphNode]) -> None:
        """Replace the displayed graph, recover the cursor by id (else reset to root), and repaint."""
        self._graph_nodes = list(nodes)
        self._rebuild()

    def set_cursor(self, path: Sequence[Hashable]) -> None:
        """Move the cursor programmatically (repaints; does NOT post `NodeHighlighted` — the caller drove it)."""
        self.cursor = tuple(path)

    # -- internals ------------------------------------------------------------

    def on_mount(self) -> None:
        self._rebuild()

    def _rebuild(self) -> None:
        """(Re)index the graph, recover the cursor, rebuild the (cached) layout, and paint."""
        self._index()
        self._layout = build_layout(self._graph_nodes) if self._graph_nodes else None
        # Silent set: a fresh graph is consumer-driven, so it must not echo a NodeHighlighted back. We still
        # paint explicitly below, since the layout changed even when the recovered cursor is unchanged.
        self.set_reactive(MergeTree.cursor, self._recovered_cursor())
        self._paint()

    def _index(self) -> None:
        self._children = {n.id: [] for n in self._graph_nodes}
        self._edges = set()
        self._root = None
        for n in self._graph_nodes:
            if not n.parents:
                self._root = n.id
            for p in n.parents:
                self._children[p].append(n.id)
                self._edges.add((p, n.id))

    def _recovered_cursor(self) -> tuple:
        # Keep the current id-path iff it is still a valid root→node path (every id present, every step an
        # edge); otherwise fall back to the root. No partial-prefix salvage — it is all-or-root.
        c = self.cursor
        valid = (bool(c) and c[0] == self._root and all(cid in self._children for cid in c)
                 and all(step in self._edges for step in zip(c, c[1:])))
        return c if valid else (() if self._root is None else (self._root,))

    def _move_to(self, path: tuple) -> None:
        """User-initiated move: update the cursor and announce it."""
        self.cursor = path
        self.post_message(self.NodeHighlighted(path, path[-1]))

    def watch_cursor(self) -> None:
        self._paint()

    def _paint(self) -> None:
        if self._layout is None:
            self.update("")
            return
        self.update(render(self._layout, self._graph_nodes, self.cursor, self._show_labels).to_text())

    # -- navigation -----------------------------------------------------------
    #
    # Siblings are taken in DISPLAY order (left→right by grid column), not graph/insertion order — the
    # layout reorders children, so "left"/"right" follows what the eye sees. left/right also "jag": if the
    # tip has no sibling that way, walk up the path to the nearest ancestor that does, swap it, and descend
    # back to the same end node if it is still reachable (else stop at the sibling). At a merge this lets you
    # swap which parent-branch you took to reach it.

    def _display_children(self, node: Hashable) -> list:
        """Children of `node` in left→right display order (by grid column)."""
        kids = self._children.get(node, [])
        return kids if self._layout is None else sorted(kids, key=lambda c: self._layout.col[c])

    def action_cursor_up(self) -> None:
        if len(self.cursor) > 1:
            self._move_to(self.cursor[:-1])                 # pop back the exact way we came in

    def action_cursor_down(self) -> None:
        kids = self._display_children(self.cursor[-1]) if self.cursor else []
        if kids:
            self._move_to(self.cursor + (kids[0],))         # the leftmost child

    def action_cursor_left(self) -> None:
        target = self._jag(-1)
        if target is not None:
            self._move_to(target)

    def action_cursor_right(self) -> None:
        target = self._jag(+1)
        if target is not None:
            self._move_to(target)

    def _jag(self, direction: int) -> tuple | None:
        """The cursor after a left (-1) / right (+1): swap at the deepest ancestor with a sibling that way,
        then either keep the same end node if it's still reachable, or descend the new branch to the same
        vertical level (a spatial feel — see :meth:`_descend_to_level`)."""
        p = self.cursor
        for i in range(len(p) - 1, 0, -1):                  # from the tip upward to the first usable branch
            siblings = self._display_children(p[i - 1])
            j = siblings.index(p[i]) + direction
            if 0 <= j < len(siblings):
                tail = self._path_down(siblings[j], p[-1])  # 1) keep the exact endpoint if reachable…
                if tail:
                    return p[:i] + tail
                return p[:i] + self._descend_to_level(siblings[j], self._layout.rank[p[-1]])  # 2) …else level
        return None

    def _path_down(self, start: Hashable, target: Hashable) -> tuple | None:
        """A shortest downward path `start → … → target` (display order), or None if target isn't below."""
        queue = deque([(start, (start,))])
        seen = {start}
        while queue:
            node, path = queue.popleft()
            if node == target:
                return path
            for c in self._display_children(node):
                if c not in seen:
                    seen.add(c)
                    queue.append((c, path + (c,)))
        return None

    def _descend_to_level(self, start: Hashable, max_rank: int) -> tuple:
        """Path from `start` down to its deepest descendant whose rank stays ≤ `max_rank` (ties: leftmost).

        When a jag swaps branches but can't reach the old endpoint, this keeps the cursor at the same
        vertical level instead of snapping shallow to the bare sibling — a spatial rather than purely
        graph-semantic feel. Ranks strictly increase downward, so "deepest within the level" is well-defined
        and pruning at `rank > max_rank` loses nothing.
        """
        if self._layout is None:
            return (start,)
        best_path = (start,)
        best_key = (self._layout.rank[start], -self._layout.col[start])
        queue = deque([(start, (start,))])
        seen = {start}
        while queue:
            node, path = queue.popleft()
            key = (self._layout.rank[node], -self._layout.col[node])    # deepest, then leftmost
            if key > best_key:
                best_key, best_path = key, path
            for c in self._display_children(node):
                if c not in seen and self._layout.rank[c] <= max_rank:
                    seen.add(c)
                    queue.append((c, path + (c,)))
        return best_path

    def action_select(self) -> None:
        if self.cursor:
            self.post_message(self.NodeSelected(self.cursor, self.cursor[-1]))

    def check_action(self, action: str, parameters: tuple) -> bool:
        # Disable (so the key bubbles to a parent) when the cursor can't move that way.
        if not self.cursor:
            return action == "select"
        if action == "cursor_up":
            return len(self.cursor) > 1
        if action == "cursor_down":
            return bool(self._children.get(self.cursor[-1]))
        if action in ("cursor_left", "cursor_right"):
            return self._has_jag(-1 if action == "cursor_left" else +1)
        return True

    def _has_jag(self, direction: int) -> bool:
        """Whether a left/right jag can move at all (cheap — no descent search)."""
        p = self.cursor
        for i in range(len(p) - 1, 0, -1):
            siblings = self._display_children(p[i - 1])
            if 0 <= siblings.index(p[i]) + direction < len(siblings):
                return True
        return False
