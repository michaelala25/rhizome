"""BrowserTabViewModel — abstract base for tabs in the browser widget.

Each concrete tab (knowledge entries, flashcards, reviews, ...) owns its own data, sort/filter
state, and rendering. The base class nails down the parts that every tab needs to share with the
orchestrator:

  * a stable ``title`` for the tab bar
  * a single ``set_topic_filter(topic_ids)`` entry point the orchestrator calls when the topic-tree
    selection changes

Most of the heavy lifting — debounce + fetch-id staleness gating — lives on
``QueryBackedViewModel``, the kernel this class inherits from. ``BrowserTabViewModel`` is a thin
layer on top: it adds the tab identity (``title``) and the topic-filter API the orchestrator
talks to. Other VMs (the linked-flashcards panel, for instance) that need the fetch protocol but
aren't tabs inherit from ``QueryBackedViewModel`` directly.
"""

from __future__ import annotations

from typing import Any, Iterable

from ..query_backed_view_model import QueryBackedViewModel


class BrowserTabViewModel(QueryBackedViewModel):
    """Abstract tab VM. Concrete subclasses must override ``_fetch``, ``_process_fetched_data``,
    and ``TITLE``, plus expose whatever data attributes the corresponding view needs to render.

    Filter semantics
    ----------------
    ``set_topic_filter(topic_ids)`` accepts:
      * ``None`` — "no topic filter" (show everything). This is the boot state and the state after
        the user clears their tree selection.
      * a (possibly empty) iterable of topic IDs — the *already-expanded* union of subtrees from
        the user's tree selection. An empty iterable means "no rows match" (selection was made but
        is empty after expansion, which is a legal terminal state).

    The orchestrator (``BrowserViewModel``) handles subtree expansion before calling here, so tabs
    never run the CTE themselves.
    """

    # Subclasses override.
    TITLE: str = "<untitled tab>"

    def __init__(self, session_factory: Any) -> None:
        super().__init__()
        self._session_factory = session_factory
        # ``None`` ≠ empty iterable; see filter semantics above.
        self._filter_ids: frozenset[int] | None = None
        # ``True`` once ``set_topic_filter`` has been called at least once. Used to distinguish
        # "filter is already None" from "filter has never been applied" — the first
        # ``set_topic_filter`` must fetch even when the requested filter happens to equal the
        # default.
        self._filter_applied: bool = False

    # ------------------------------------------------------------------
    # Read-only view-side accessors
    # ------------------------------------------------------------------

    @property
    def title(self) -> str:
        return self.TITLE

    @property
    def filter_ids(self) -> frozenset[int] | None:
        return self._filter_ids

    # ------------------------------------------------------------------
    # Orchestrator-facing API
    # ------------------------------------------------------------------

    def set_topic_filter(self, topic_ids: Iterable[int] | None) -> None:
        """Set the active topic filter and (re)fetch if it actually changed.

        Idempotent: calling with the same filter the tab already holds is a no-op (no cancel, no
        fetch, no dirty emit). This matters under lazy propagation in the orchestrator —
        switching to a tab that's already showing data for the current filter should be instant,
        not paint a loading flash.

        Coalescing across rapid distinct filters is handled by the debounce in
        ``QueryBackedViewModel._request_fetch``; tabs don't need to know about input cadence.
        """
        new_filter: frozenset[int] | None = (
            None if topic_ids is None else frozenset(topic_ids)
        )
        if self._filter_applied and new_filter == self._filter_ids:
            return
        self._filter_ids = new_filter
        self._filter_applied = True
        self._request_fetch()
