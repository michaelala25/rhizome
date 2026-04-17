"""ResourceManager — tracks loaded resource state and computes diffs for the agent session.

State is represented in **minimum-description-length (MDL) form**: a flat
``dict[NodeKey, LoadMode]`` where an entry at node *X* means "*X* and every
descendant of *X* are loaded at this mode, unless a descendant has its own
entry overriding it."  A node that is UNLOADED simply has no entry.

The loader maintains this invariant — at any moment, no two entries in the
dict have one as an ancestor of the other at the *same* mode (such pairs are
always collapsed to a single parent entry).  Entries at different modes may
coexist: the descendant overrides the ancestor for its subtree.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Literal

from rhizome.logs import get_logger

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


@dataclass(frozen=True)
class ResourceStateChange:
    """A single per-key change between two consecutive manager snapshots.

    ``old_mode`` / ``new_mode`` is ``None`` when the key was absent from the
    corresponding state.  Note that because of MDL expansion/collapse, a
    single user action can produce several of these changes — interpret them
    as a transactional batch describing the new MDL state, not as semantic
    "effective mode changed" events.
    """

    node: NodeKey
    old_mode: LoadMode | None
    new_mode: LoadMode | None


def _fmt_state(state: dict[NodeKey, LoadMode]) -> str:
    if not state:
        return "(empty)"
    parts = [f"{kind[0]}{nid}:{mode.value}" for (kind, nid), mode in sorted(state.items())]
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class ResourceManager:
    """Tracks MDL load state and provides diffs to the agent session.

    The ResourceLoader pushes full snapshots via :meth:`set_state` on every
    user interaction.  The manager stores the snapshot as ``_next`` and only
    computes a diff against ``_current`` when the agent session calls
    :meth:`consume` at the start of each stream.
    """

    def __init__(self, session_factory=None) -> None:
        self._session_factory = session_factory
        self._current: dict[NodeKey, LoadMode] = {}
        self._next: dict[NodeKey, LoadMode] = {}
        self._embedding_in_progress: set[int] = set()

    # ------------------------------------------------------------------
    # State updates (called by the UI layer)
    # ------------------------------------------------------------------

    def set_state(self, state: dict[NodeKey, LoadMode]) -> None:
        """Replace the next state wholesale with a snapshot from the loader."""
        new_next = dict(state)
        if new_next != self._next:
            self._next = new_next
            _log.debug("State updated: %s", _fmt_state(self._next))

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
        from rhizome.resources.embeddings import has_embeddings, compute_embeddings

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

    def consume(self) -> list[ResourceStateChange]:
        """Return the key-level diff since the last ``consume()`` and freeze next as current.

        After this call ``_current == _next``, so a subsequent ``consume()``
        with no intervening state updates returns an empty list.
        """
        changes: list[ResourceStateChange] = []
        all_keys = set(self._current) | set(self._next)
        for key in sorted(all_keys):
            old = self._current.get(key)
            new = self._next.get(key)
            if old != new:
                changes.append(ResourceStateChange(node=key, old_mode=old, new_mode=new))

        self._current = dict(self._next)
        if changes:
            _log.info(
                "Consumed %d change(s): %s",
                len(changes),
                ", ".join(
                    f"{c.node[0][0]}{c.node[1]}:{_fmt_mode(c.old_mode)}->{_fmt_mode(c.new_mode)}"
                    for c in changes
                ),
            )
        else:
            _log.debug("Consumed with no pending changes")
        return changes


def _fmt_mode(mode: LoadMode | None) -> str:
    return mode.value if mode is not None else "-"
