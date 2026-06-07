"""Abstract base for tabs in the browser widget. Declares the surface the orchestrator and view
need without prescribing how a tab implements its data layer.

Concrete tabs own their data, sort/filter state, and rendering. The base only nails down what the
orchestrator needs: a stable ``TITLE`` for the tab strip, plus ``set_topic_filter`` / ``refetch``
entry points for routing the tree's selection and rerunning the query after out-of-band data
changes.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Iterable

from rhizome.app.model import ViewModelBase


class BrowserTabModel(ViewModelBase):
    """Abstract tab VM. Subclasses define ``TITLE`` and implement ``set_topic_filter`` /
    ``refetch``. The base inherits from ``ViewModelBase`` so concrete tabs get the standard
    ``OnDirty`` channel without a parallel base hierarchy.

    ``set_topic_filter`` takes the *already-expanded* union of topic ids (the orchestrator runs the
    subtree CTE). ``None`` means "no filter, show everything"; an empty iterable means "selection
    expanded to zero rows" — both are legal, distinct terminal states preserved end-to-end.
    """

    TITLE: str = "<untitled tab>"

    @property
    def title(self) -> str:
        return self.TITLE

    @abstractmethod
    def set_topic_filter(self, topic_ids: Iterable[int] | None) -> None:
        """Set the active topic filter. Idempotent on equal filters — needed so the orchestrator's
        lazy tab catch-up doesn't paint a loading flash when switching to a tab that already
        matches the current filter."""
        raise NotImplementedError

    @abstractmethod
    def refetch(self) -> None:
        """Re-run the current query without changing inputs. Used by the orchestrator after
        out-of-band data changes (e.g. a topic rename) that may have invalidated cached rows."""
        raise NotImplementedError
