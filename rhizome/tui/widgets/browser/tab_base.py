"""Abstract base for tabs in the browser widget — adds tab identity and topic-filter wiring on top
of ``QueryBackedViewModel`` (which already provides the debounce + fetch-id staleness kernel).

Concrete tabs own their data, sort/filter state, and rendering. The base only nails down what the
orchestrator needs: a stable ``TITLE`` for the tab bar and ``set_topic_filter`` for routing the
tree's selection. Non-tab VMs that need the same fetch kernel (e.g. the linked-flashcards panel)
inherit from ``QueryBackedViewModel`` directly rather than from this class.
"""

from __future__ import annotations

from typing import Any, Iterable

from ..query_backed_view_model import QueryBackedViewModel


class BrowserTabViewModel(QueryBackedViewModel):
    """Abstract tab VM. Subclasses override ``TITLE``, ``_fetch``, and ``_process_fetched_data``.

    ``set_topic_filter`` takes the *already-expanded* union of topic ids (the orchestrator runs the
    subtree CTE). ``None`` means "no filter, show everything"; an empty iterable means "selection
    expanded to zero rows" — both are legal, distinct terminal states preserved end-to-end.
    """

    TITLE: str = "<untitled tab>"

    def __init__(self, session_factory: Any) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._filter_ids: frozenset[int] | None = None
        # Distinguishes "filter is None by default" from "filter has never been set" — the first
        # call must fetch even when the requested filter happens to equal the default.
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
        """Set the active topic filter and (re)fetch if it actually changed. Idempotent on equal
        filters — needed so the orchestrator's lazy tab catch-up doesn't paint a loading flash when
        switching to a tab that already matches the current filter."""
        new_filter: frozenset[int] | None = None if topic_ids is None else frozenset(topic_ids)
        if self._filter_applied and new_filter == self._filter_ids:
            return
        self._filter_ids = new_filter
        self._filter_applied = True
        self._request_fetch()

    def refetch(self) -> None:
        """Re-run the current query without changing inputs. Used by the orchestrator after
        out-of-band data changes (e.g. a topic rename) that may have invalidated cached rows."""
        self._request_fetch()
