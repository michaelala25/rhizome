"""The translation step: ``ConversationGraph`` → a *display DAG* the merge-tree widget can paint.

The conversation graph and the merge-tree renderer both sit on the same ``MergeTree`` data structure, but
what the viewer shows is not the conversation graph verbatim — it is a *projection* of it, parameterised
by a :class:`Mode`:

- **COLLAPSED** — one display node per conversation node (the quotient DAG, so a merged node appears
  once). A node that *forked* (≥2 children) additionally gets a dedicated branch-point node spliced
  between it and its children, so "this is where the branch happened" reads as its own marker rather than
  being implied by the bottom of a conversation node.
- **EXPANDED** — each conversation node blows up into a vertical chain of message / agent-run nodes
  (lands in a later increment).

Because the display DAG is not 1:1 with the conversation graph, every :class:`DisplayNode` keeps a
back-reference (``node_id`` + ``kind``) to what it maps to, so the layer above can quick-nav and preview.

Stable, disjoint ids
--------------------
The widget recovers its path-cursor across a ``set_graph`` *by id*, so display ids must be stable across
rebuilds and disjoint across kinds. We use tagged tuples — ``("node", nid)`` / ``("branch", nid)`` (and,
in expanded mode, ``("msg", item_id)`` / ``("run", item_id)``) — keyed on the conversation node id or the
globally-unique ``ConversationItem`` id, both of which are stable for the lifetime of what they name.

This module is pure: it reads the graph through its public surface (``root`` / ``children`` / ``node`` /
``cursor``) and returns plain values, so it is testable without a view.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Hashable

if TYPE_CHECKING:
    from rhizome.app.chat_area.conversation_graph import ConversationGraph, ConversationNode, Cursor


class Mode(Enum):
    """Which projection is live. The mode is *business state* (it selects which set of display nodes
    exists), so it lives on the view-model rather than being a view-side toggle."""

    COLLAPSED = "collapsed"
    EXPANDED = "expanded"


class DisplayKind(Enum):
    """What a display node represents — the semantic kind the view maps to a marker / style. Presentation
    (glyph, colour) is deliberately *not* here: that is the view's call."""

    CONVERSATION = "conversation"   # one whole conversation node (collapsed mode)
    BRANCH_POINT = "branch_point"   # the fork marker spliced below a node that branched
    USER_MESSAGE = "user_message"   # one genuine user message (expanded mode)
    AGENT_RUN    = "agent_run"      # one contiguous agent run — answers + tool calls (expanded mode)


@dataclass(frozen=True)
class DisplayNode:
    """One node of the display DAG, plus its back-reference into the conversation graph.

    ``id`` / ``parent_ids`` are what the widget lays out; ``label`` / ``kind`` drive presentation;
    ``node_id`` is the conversation node this maps to (the quick-nav target and the preview source);
    ``is_current`` marks the node the chat's cursor currently sits on (the "you are here" marker).
    """

    id: Hashable
    parent_ids: tuple[Hashable, ...]
    label: str
    kind: DisplayKind
    node_id: int
    is_current: bool = False


# ======================================================================================================
# PROJECTION
# ======================================================================================================

def build_display_nodes(graph: ConversationGraph, cursor: Cursor, mode: Mode) -> list[DisplayNode]:
    """Project ``graph`` (with the chat's current ``cursor``) into the display DAG for ``mode``."""
    if mode is Mode.COLLAPSED:
        return _collapsed(graph, cursor)
    raise NotImplementedError("expanded-mode projection lands in a later increment")


def _collapsed(graph: ConversationGraph, cursor: Cursor) -> list[DisplayNode]:
    """One display node per conversation node, with a branch-point node spliced below every fork.

    Walks the topology from the root through the public ``children`` API (so we never reach for the
    graph's private tree), recording every parent→child edge and a stable emission order. A merged node
    (reachable two ways) is emitted once but collects *both* parents, so the widget draws the convergence;
    its in-degree ≥ 2 is what makes the widget render it as a merge.
    """
    leaf_id = cursor.node.id
    root = graph.root

    seen: set[int] = set()
    order: list[ConversationNode] = []
    parents_of: dict[int, list[int]] = {}

    def visit(node: ConversationNode) -> None:
        if node.id in seen:
            return
        seen.add(node.id)
        order.append(node)
        # Record the edge to every child (even an already-seen merge child, so it keeps both parents in
        # arrival order), then recurse — the seen-guard keeps each subtree's edges recorded exactly once.
        for child in graph.children(node):
            parents_of.setdefault(child.id, []).append(node.id)
            visit(child)

    visit(root)

    # A fork is a node with ≥2 children; its children attach to its branch-point node, not to it directly.
    forked: set[int] = {n.id for n in order if len(graph.children(n)) >= 2}

    def parent_display_id(pid: int) -> Hashable:
        return ("branch", pid) if pid in forked else ("node", pid)

    display: list[DisplayNode] = []
    for node in order:
        display.append(DisplayNode(
            id=("node", node.id),
            parent_ids=tuple(parent_display_id(pid) for pid in parents_of.get(node.id, [])),
            label=node.name or f"#{node.id}",
            kind=DisplayKind.CONVERSATION,
            node_id=node.id,
            is_current=node.id == leaf_id,
        ))
        # The fork marker sits directly below the node it belongs to; the node's children point at it.
        if node.id in forked:
            display.append(DisplayNode(
                id=("branch", node.id),
                parent_ids=(("node", node.id),),
                label="",
                kind=DisplayKind.BRANCH_POINT,
                node_id=node.id,
            ))

    return display
