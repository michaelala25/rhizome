"""Commit-mode payload assembly — the pure transformation from a cross-branch message selection into
the ``commit_proposal_state.payload`` the agent's commit tools read off state.

The *stateful* side of commit mode (the active flag, the selection set, the enter/exit/toggle/submit
lifecycle) lives on ``ChatAreaModel``. What lives here is side-effect-free: the eligibility policy
(which feed entries may be staged) and the assembly (folding a selection into the list-of-dicts the
agent ingests). Kept separate so the chat-area VM stays orchestration and this stays testable alone.

A payload entry is a plain dict the agent reads back via ``commit_show_selected_messages``::

    {"content": str, "role": str, "branch": str | None, "user_context"?: str}

``branch`` is the source node's name — provenance that is newly meaningful now that a single commit
can gather messages from several branches.
"""

from __future__ import annotations

from rhizome.app.chat_area.messages.agent import AgentMessageModel
from rhizome.app.chat_area.messages.static import ChatMessageModel
from rhizome.tui.types import Mode, Role

from .conversation_graph import ConversationGraph, ConversationItem, ConversationNode


# Selection-eligibility level — the vocabulary of ``Options.CommitSelectable``. The chat-area VM reads
# that option and passes the level in; ``DEFAULT_LEVEL`` is only the fallback for a standalone area with
# no option service in scope.
#   "learn_only" — agent answers produced in learn mode only
#   "all_agent"  — every agent answer, any mode
#   "all"        — agent answers plus the user's own messages
DEFAULT_LEVEL = "all_agent"


def is_selectable(entry: object, *, level: str = DEFAULT_LEVEL) -> bool:
    """Whether a feed entry may be staged for commit under ``level``.

    Thinking segments (adaptive-thinking summaries) and unfinished/cancelled agent segments are never
    eligible. Otherwise an ``AgentMessageModel`` qualifies as an answer (gated to learn mode under
    ``learn_only``); a USER ``ChatMessageModel`` qualifies only under ``all``.
    """
    if isinstance(entry, AgentMessageModel):
        if entry.thinking or entry.cancelled or entry.streaming:
            return False
        if level == "learn_only":
            return entry.mode == Mode.LEARN
        return True
    if isinstance(entry, ChatMessageModel):
        return level == "all" and entry.role == Role.USER
    return False


def build_payload(
    graph: ConversationGraph,
    selection: dict[int, tuple[ConversationNode, ConversationItem]],
) -> list[dict]:
    """Fold a cross-branch selection into the ``commit_proposal_state.payload`` list, in graph order.

    Walks the whole topology root-to-leaf (preorder over branches, via the public ``children`` API) so
    entries read in conversational order regardless of the order they were checked off, then emits one
    dict per selected entry. Agent answers carry the nearest preceding user message on their own branch
    as ``user_context`` (the prompt that elicited them), when one is present.
    """
    payload: list[dict] = []
    seen: set[int] = set()
    stack: list[ConversationNode] = [graph.root]
    while stack:
        node = stack.pop()
        if node.id in seen:
            continue
        seen.add(node.id)
        for item in node.feed:
            if item.id in selection:
                entry = _entry_to_dict(node, item)
                if entry is not None:
                    payload.append(entry)
        # Children pushed reversed so they pop in creation order — a left-to-right preorder walk. A
        # merge node (reachable two ways) is guarded by ``seen``; item ids are globally unique anyway.
        for child in reversed(graph.children(node)):
            stack.append(child)
    return payload


def _entry_to_dict(node: ConversationNode, item: ConversationItem) -> dict | None:
    text, role = _text_and_role(item.entry)
    if text is None:
        return None
    out: dict = {"content": text, "role": role, "branch": node.name}
    if isinstance(item.entry, AgentMessageModel):
        context = _preceding_user_context(node, item)
        if context:
            out["user_context"] = context
    return out


def _text_and_role(entry: object) -> tuple[str | None, str]:
    if isinstance(entry, AgentMessageModel):
        return entry.body, Role.AGENT.value
    if isinstance(entry, ChatMessageModel):
        return entry.content, entry.role.value
    return None, ""


def _preceding_user_context(node: ConversationNode, item: ConversationItem) -> str | None:
    """The nearest USER message preceding ``item`` in ``node``'s own feed — the prompt that elicited an
    answer. Stops at an earlier agent answer (whose own prompt is not this one's context).

    TODO(cross-branch): when the prompting user turn lives on an ancestor branch (a segment that opens
    with an answer), this finds nothing; walking up the lineage would recover it.
    """
    feed = node.feed
    index = next((i for i, it in enumerate(feed) if it.id == item.id), None)
    if index is None:
        return None
    for previous in reversed(feed[:index]):
        entry = previous.entry
        if isinstance(entry, ChatMessageModel) and entry.role == Role.USER:
            return entry.content
        if isinstance(entry, AgentMessageModel):
            return None
    return None
