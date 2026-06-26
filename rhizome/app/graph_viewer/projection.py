"""The translation step: ``ConversationGraph`` → a *display DAG* the merge-tree widget can paint.

The conversation graph and the merge-tree renderer both sit on the same ``MergeTree`` data structure, but
what the viewer shows is not the conversation graph verbatim — it is a *projection* of it, parameterised
by a :class:`Mode`:

- **COLLAPSED** — one display node per conversation node (the quotient DAG, so a merged node appears
  once). A node that *forked* (≥2 children) additionally gets a dedicated branch-point node spliced
  between it and its children, so "this is where the branch happened" reads as its own marker rather than
  being implied by the bottom of a conversation node.
- **EXPANDED** — each conversation node blows up into a vertical chain of *chunks*: one per user message
  and one per *agent run* (a maximal contiguous block of agent text + tool-call items), in conversation
  order, chained by "came-before" edges. A node with no such items yet collapses to a single conversation
  chunk so its children's edges still land somewhere. Same fork/merge skeleton as COLLAPSED.

Because the display DAG is not 1:1 with the conversation graph, every :class:`DisplayNode` keeps a
back-reference (``node_id`` + ``kind``) to what it maps to, plus the ``item_ids`` it covers, so the layer
above can quick-nav (scroll to the first item) and, later, preview.

What counts as a chunk (expanded mode)
--------------------------------------
An *allowlist*, so a new feed-item type is transparent by default rather than needing to be excluded:

    USER_MESSAGE   a ``ChatMessageModel`` with ``role is Role.USER`` (shell ``!`` commands and slash-
                   command output are other types / other roles, so they fall out for free)
    AGENT_RUN      a maximal run of ``AgentMessageModel`` / ``ToolMessageModel`` items
    (everything else — thinking indicators, interrupts, system notices, welcome banners, … — is
    transparent: it neither makes a chunk nor breaks a run)

Branch points come from the *topology* (a node with ≥2 children), not from the ``BranchPointModel`` that
also sits in the feed; that entry is just the scroll target quick-nav looks up.

Stable, disjoint ids
--------------------
The widget recovers its path-cursor across a ``set_graph`` *by id*, so display ids must be stable across
rebuilds and disjoint across kinds. We use tagged tuples — ``("node", nid)`` / ``("branch", nid)`` /
``("msg", item_id)`` / ``("run", first_item_id)`` — keyed on the conversation node id or the globally-
unique ``ConversationItem`` id, both of which are stable for the lifetime of what they name.

This module is pure: it reads the graph through its public surface (``root`` / ``children`` / ``node`` /
``cursor``) plus each node's ``feed``, and returns plain values, so it is testable without a view.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Hashable, Sequence

from rhizome.app.chat_area.messages.agent import AgentMessageModel
from rhizome.app.chat_area.messages.static import ChatMessageModel
from rhizome.app.chat_area.messages.tool import ToolMessageModel
from rhizome.tui.types import Role

if TYPE_CHECKING:
    from rhizome.app.chat_area.conversation_graph import ConversationGraph, ConversationItem, ConversationNode, Cursor


class Mode(Enum):
    """Which projection is live. The mode is *business state* (it selects which set of display nodes
    exists), so it lives on the view-model rather than being a view-side toggle."""

    COLLAPSED = "collapsed"
    EXPANDED = "expanded"


class DisplayKind(Enum):
    """What a display node represents — the semantic kind the view maps to a marker / style. Presentation
    (glyph, colour) is deliberately *not* here: that is the view's call."""

    CONVERSATION = "conversation"   # one whole conversation node (collapsed mode, or an empty node expanded)
    BRANCH_POINT = "branch_point"   # the fork marker spliced below a node that branched
    USER_MESSAGE = "user_message"   # one genuine user message (expanded mode)
    AGENT_RUN    = "agent_run"      # one contiguous agent run — answers + tool calls (expanded mode)


@dataclass(frozen=True)
class DisplayNode:
    """One node of the display DAG, plus its back-reference into the conversation graph.

    ``id`` / ``parent_ids`` are what the widget lays out; ``kind`` drives presentation; ``preview`` is the
    node's source text (a branch name for a conversation/branch node, the message text for a chunk) — the
    view derives both the clipped diagram chip and the head/tail preview box from it. ``node_id`` is the
    conversation node this maps to (the quick-nav target); ``item_ids`` are the feed items it covers
    (quick-nav scrolls to the first); ``is_current`` marks the node the chat's cursor sits on (the "you are
    here" marker — in expanded mode, the node's final chunk).
    """

    id: Hashable
    parent_ids: tuple[Hashable, ...]
    preview: str
    kind: DisplayKind
    node_id: int
    is_current: bool = False
    item_ids: tuple[int, ...] = ()


# ======================================================================================================
# PROJECTION
# ======================================================================================================

def build_display_nodes(graph: ConversationGraph, cursor: Cursor, mode: Mode) -> list[DisplayNode]:
    """Project ``graph`` (with the chat's current ``cursor``) into the display DAG for ``mode``."""
    if mode is Mode.COLLAPSED:
        return _collapsed(graph, cursor)
    return _expanded(graph, cursor)


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
            preview=node.name or f"#{node.id}",
            kind=DisplayKind.CONVERSATION,
            node_id=node.id,
            is_current=node.id == leaf_id,
        ))
        # The fork marker sits directly below the node it belongs to; the node's children point at it.
        if node.id in forked:
            display.append(DisplayNode(
                id=("branch", node.id),
                parent_ids=(("node", node.id),),
                preview="",
                kind=DisplayKind.BRANCH_POINT,
                node_id=node.id,
            ))

    return display


# ======================================================================================================
# EXPANDED PROJECTION
# ======================================================================================================

@dataclass(frozen=True)
class _Chunk:
    """One expanded-mode chunk before it is wired into the DAG — a node's feed reduces to an ordered list
    of these (its parents/edges are decided later, by position in the conversation)."""

    id: Hashable
    kind: DisplayKind
    preview: str
    item_ids: tuple[int, ...]


def _oneline(text: str) -> str:
    """Collapse a message body to a single whitespace-normalised line. This is the *source* text — the
    view clips it for the diagram chip and head/tail-compacts it for the preview box; we deliberately do
    not render its content (the feed's own message widgets do that, math and all)."""
    return " ".join(text.split())


def _run_preview(entries: Sequence) -> str:
    """Preview source for an agent run: its answer segments (non-thinking agent text) joined, else the
    tool names it called, else a bare ``"agent"``. The whole source is returned — bounding it to a glance
    is the view's call (it shows the head and tail, not the middle)."""
    answers = [_oneline(entry.body) for entry in entries
               if isinstance(entry, AgentMessageModel) and not entry.thinking and entry.body.strip()]
    if answers:
        return " ".join(answers)
    tools = [name for entry in entries if isinstance(entry, ToolMessageModel) for name, _ in entry.tools]
    if tools:
        return ", ".join(tools)
    return "agent"


def _node_chunks(node: ConversationNode) -> list[_Chunk]:
    """Reduce one node's feed to ordered chunks (see the allowlist in the module docstring): each user
    message is its own chunk; a maximal contiguous block of agent/tool items coalesces into one agent-run
    chunk; every other feed item is transparent (skipped, and does not break a run)."""
    chunks: list[_Chunk] = []
    run: list[ConversationItem] = []

    def flush() -> None:
        if run:
            chunks.append(_Chunk(
                id=("run", run[0].id),
                kind=DisplayKind.AGENT_RUN,
                preview=_run_preview([it.entry for it in run]),
                item_ids=tuple(it.id for it in run),
            ))
            run.clear()

    for item in node.feed:
        entry = item.entry
        if isinstance(entry, ChatMessageModel) and entry.role is Role.USER:
            flush()
            chunks.append(_Chunk(
                id=("msg", item.id),
                kind=DisplayKind.USER_MESSAGE,
                preview=_oneline(entry.content),
                item_ids=(item.id,),
            ))
        elif isinstance(entry, (AgentMessageModel, ToolMessageModel)):
            run.append(item)
        # else: transparent — neither a chunk nor a run break.

    flush()
    return chunks


def _expanded(graph: ConversationGraph, cursor: Cursor) -> list[DisplayNode]:
    """Each conversation node becomes a vertical chain of chunks (one per user message / agent run),
    chained by came-before edges, with a branch-point chunk spliced below every fork — the same skeleton
    as :func:`_collapsed`, only a node expands into a run of chunks instead of a single node.

    An empty node (no user/agent items yet — e.g. a freshly-branched leaf) falls back to a single
    CONVERSATION chunk so its children's edges still land somewhere and it stays selectable; that makes
    collapsed the degenerate "every node is one chunk" case of this.
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
        for child in graph.children(node):
            parents_of.setdefault(child.id, []).append(node.id)
            visit(child)

    visit(root)

    forked: set[int] = {n.id for n in order if len(graph.children(n)) >= 2}

    # Pass 1: a node's chunk chain depends only on its own feed, so resolve every node's chain (and hence
    # its final chunk) up front — then a merge child can attach to either parent's final chunk regardless
    # of which one the DFS reached first.
    chunks_of: dict[int, list[_Chunk]] = {}
    for node in order:
        chunks_of[node.id] = _node_chunks(node) or [
            _Chunk(("node", node.id), DisplayKind.CONVERSATION, node.name or f"#{node.id}", ())
        ]
    final_chunk: dict[int, Hashable] = {nid: chunks[-1].id for nid, chunks in chunks_of.items()}

    def attach_ids(node: ConversationNode) -> tuple[Hashable, ...]:
        """What a node's *first* chunk hangs off: per parent, its branch point if it forked, else its
        final chunk — the exact analog of collapsed's ``("branch", pid)`` / ``("node", pid)`` rule."""
        return tuple(("branch", pid) if pid in forked else final_chunk[pid]
                     for pid in parents_of.get(node.id, []))

    # Pass 2: emit chunks in conversation order, each pointing at the previous chunk in its node (or, for
    # the first, at what the parent handed down), with the fork marker spliced below the node's final chunk.
    display: list[DisplayNode] = []
    for node in order:
        chunks = chunks_of[node.id]
        prev: Hashable | None = None
        for i, chunk in enumerate(chunks):
            display.append(DisplayNode(
                id=chunk.id,
                parent_ids=(prev,) if prev is not None else attach_ids(node),
                preview=chunk.preview,
                kind=chunk.kind,
                node_id=node.id,
                is_current=(node.id == leaf_id and i == len(chunks) - 1),
                item_ids=chunk.item_ids,
            ))
            prev = chunk.id

        if node.id in forked:
            display.append(DisplayNode(
                id=("branch", node.id),
                parent_ids=(prev,),
                preview="",
                kind=DisplayKind.BRANCH_POINT,
                node_id=node.id,
            ))

    return display
