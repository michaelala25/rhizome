"""ResourceManager — tracks loaded resource state and produces messages for the agent session.

State is represented in **minimum-description-length (MDL) form**: a flat
``dict[NodeKey, LoadMode]`` where an entry at node *X* means "*X* and every
descendant of *X* are loaded at this mode, unless a descendant has its own
entry overriding it."

The loader pushes full snapshots via :meth:`set_state` on every user toggle.
The agent session calls :meth:`consume` at the start of each stream, which
diffs the current snapshot against the last-consumed snapshot and returns a
list of messages to inject into the graph state:

- For every resource whose context-stuffed (CS) entry set changed, emit one
  :class:`HumanMessage` with deterministic id
  ``rhizome-resource-ctx-{resource_id}``.  The ``add_messages`` reducer
  replaces prior messages with the same id in place, so toggling CS'd
  sections updates content without appending duplicates.
- For every resource that had CS content before and has none now, emit a
  :class:`RemoveMessage` with the same id to drop it from graph state.

The manager maintains a cumulative ``_section_owner`` map (section_id →
resource_id), updated on every :meth:`set_state` call.  This lets us emit
correct RemoveMessages even for sections that were deleted from the DB
between the last ``set_state`` and the current ``consume``.
"""

from __future__ import annotations

import enum
from typing import Literal

from langchain_core.messages import BaseMessage, RemoveMessage

from rhizome.db.operations import get_resource_with_content_and_sections
from rhizome.logs import get_logger
from rhizome.resources.context_message import (
    build_resource_context_message,
    resource_context_message_id,
)
from rhizome.resources.embeddings import compute_embeddings, has_embeddings

_log = get_logger("resources.manager")


# ---------------------------------------------------------------------------
# State representation
# ---------------------------------------------------------------------------

class LoadMode(enum.Enum):
    """How a resource or section is loaded for the agent."""

    LOADED = "loaded"
    CONTEXT_STUFFED = "context_stuffed"


NodeKind = Literal["resource", "section"]
NodeKey = tuple[NodeKind, int]


def _fmt_state(state: dict[NodeKey, LoadMode]) -> str:
    if not state:
        return "(empty)"
    parts = [f"{kind[0]}{nid}:{mode.value}" for (kind, nid), mode in sorted(state.items())]
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class ResourceManager:
    """Tracks MDL load state and produces context-stuffing messages."""

    def __init__(self, session_factory=None) -> None:
        self._session_factory = session_factory
        self._current: dict[NodeKey, LoadMode] = {}
        self._next: dict[NodeKey, LoadMode] = {}
        # Accumulates all section → resource ownerships ever pushed.  Never
        # cleared, so RemoveMessages work for sections deleted from the DB.
        self._section_owner: dict[int, int] = {}
        self._embedding_in_progress: set[int] = set()

    # ------------------------------------------------------------------
    # State updates (called by the UI layer)
    # ------------------------------------------------------------------

    def set_state(
        self,
        state: dict[NodeKey, LoadMode],
        section_owners: dict[int, int] | None = None,
    ) -> None:
        """Replace the next state wholesale with a snapshot from the loader.

        ``section_owners`` is a ``{section_id: resource_id}`` mapping covering
        every section in ``state``; the manager merges it into its cumulative
        ownership table.  Passing the owner map avoids DB round-trips at
        consume time and handles the case where a section is later deleted.
        """
        new_next = dict(state)
        if new_next != self._next:
            self._next = new_next
            _log.debug("State updated: %s", _fmt_state(self._next))
        if section_owners:
            self._section_owner.update(section_owners)

    # ------------------------------------------------------------------
    # Embedding lifecycle
    # ------------------------------------------------------------------

    def is_embedding_in_progress(self, resource_id: int) -> bool:
        """True if an embedding computation is in-flight for this resource."""
        return resource_id in self._embedding_in_progress

    async def ensure_embedded(self, resource_id: int) -> bool:
        """Check for embeddings and compute them if missing.

        Returns ``True`` on success (embeddings now exist), ``False`` on
        failure (API error, missing raw_text, etc.).

        The caller is responsible for running this as an async task or
        Textual worker.
        """
        self._embedding_in_progress.add(resource_id)
        try:
            if await has_embeddings(self._session_factory, resource_id):
                _log.info("Resource %d already has embeddings", resource_id)
                return True

            _log.info("Computing embeddings for resource %d ...", resource_id)
            await compute_embeddings(self._session_factory, resource_id)
            _log.info("Embeddings complete for resource %d", resource_id)
            return True
        except Exception:
            _log.exception("Embedding failed for resource %d", resource_id)
            return False
        finally:
            self._embedding_in_progress.discard(resource_id)

    # ------------------------------------------------------------------
    # Consumption (called by AgentSession.stream)
    # ------------------------------------------------------------------

    async def consume(self) -> list[BaseMessage]:
        """Diff ``_current`` vs ``_next`` and return messages for the graph.

        Emits one HumanMessage per resource whose CS entry set changed (new
        content or replacement of existing content), and one RemoveMessage
        per resource that lost all its CS entries.  Advances ``_current`` to
        ``_next`` after producing the diff.
        """
        old_by_rid = self._group_cs_by_resource(self._current)
        new_by_rid = self._group_cs_by_resource(self._next)

        removals: list[int] = []
        rebuilds: list[int] = []
        for rid in set(old_by_rid) | set(new_by_rid):
            old_entries = old_by_rid.get(rid) or []
            new_entries = new_by_rid.get(rid) or []
            if old_entries == new_entries:
                continue
            if not new_entries:
                removals.append(rid)
            else:
                rebuilds.append(rid)

        messages: list[BaseMessage] = []

        for rid in sorted(removals):
            messages.append(RemoveMessage(id=resource_context_message_id(rid)))

        if rebuilds:
            if self._session_factory is None:
                _log.warning(
                    "ResourceManager has no session_factory; skipping %d content fetch(es)",
                    len(rebuilds),
                )
            else:
                async with self._session_factory() as session:
                    for rid in sorted(rebuilds):
                        resource = await get_resource_with_content_and_sections(session, rid)
                        if resource is None:
                            _log.warning(
                                "Resource %d not found while building context message", rid,
                            )
                            continue
                        msg = build_resource_context_message(resource, new_by_rid[rid])
                        if msg is not None:
                            messages.append(msg)

        self._current = dict(self._next)

        if messages:
            _log.info(
                "Consumed: %d msg(s) (%d rebuild, %d remove)",
                len(messages), len(rebuilds), len(removals),
            )
        else:
            _log.debug("Consumed with no pending messages")
        return messages

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _owner_of(self, key: NodeKey) -> int | None:
        """Resolve a NodeKey to its owning resource id."""
        kind, nid = key
        if kind == "resource":
            return nid
        return self._section_owner.get(nid)

    def _group_cs_by_resource(
        self, state: dict[NodeKey, LoadMode],
    ) -> dict[int, list[NodeKey]]:
        """Group CS-mode entries under a snapshot by owning resource id."""
        grouped: dict[int, list[NodeKey]] = {}
        for key, mode in state.items():
            if mode != LoadMode.CONTEXT_STUFFED:
                continue
            rid = self._owner_of(key)
            if rid is None:
                _log.warning("Cannot resolve owner for %r; skipping", key)
                continue
            grouped.setdefault(rid, []).append(key)
        # Sort entries within each resource so equality comparison is stable.
        for rid in grouped:
            grouped[rid].sort()
        return grouped
