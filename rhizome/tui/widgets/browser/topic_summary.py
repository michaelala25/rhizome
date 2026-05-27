"""TopicSummaryViewModel + TopicSummaryView — read-only summary panel for the cursor-highlighted topic.

Sits below the topic tree in the left rail of the browser. The orchestrator pushes the tree's cursor
topic id into the VM via ``set_topic_id``; the VM fetches the topic row plus direct/subtree entry and
flashcard counts and exposes them for the view to render.

VM is a ``QueryBackedViewModel`` so we get the standard debounce + supersede-on-restart behaviour for
free — fast cursor scrolling through the tree collapses into a single eventual query rather than
hammering the DB with one fetch per arrow keypress.

Counts come in two flavours per resource type:

  * **direct** — rows whose ``topic_id`` equals the cursor topic. Cheap (one indexed COUNT each).
  * **subtree** — rows whose ``topic_id`` is anywhere in the subtree rooted at the cursor topic. Pays
    one extra recursive-CTE expansion (``expand_subtrees``) plus an ``IN``-COUNT each. For leaf topics
    the subtree is a single id so the counts coincide; we still run the query — distinguishing leaves
    statically would require an extra ``find_parent_topic_ids`` and the equivalence is harmless.

Empty state: when ``set_topic_id(None)`` is called (or the tree has no cursor), the VM clears its
loaded fields and the view shows a placeholder. ``set_topic_id`` is idempotent — no-op + no fetch if
the id is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rich.text import Text

from textual.containers import Vertical
from textual.widgets import Static

from rhizome.db import Topic
from rhizome.db.operations import (
    count_entries,
    count_entries_filtered,
    count_flashcards_by_topic,
    count_flashcards_by_topics,
    expand_subtrees,
    get_topic,
)
from rhizome.logs import get_logger

from ..query_backed_view_model import QueryBackedViewModel

_logger = get_logger("browser.topic_summary")


@dataclass(frozen=True)
class _Summary:
    """Snapshot returned by ``_fetch`` and applied by ``_process_fetched_data``."""
    topic: Topic | None
    direct_entries: int
    subtree_entries: int
    direct_flashcards: int
    subtree_flashcards: int


class TopicSummaryViewModel(QueryBackedViewModel):
    """VM for the topic summary panel.

    Single input — ``_topic_id`` — and a snapshot of summary fields produced by ``_fetch``. The
    orchestrator drives the input via ``set_topic_id`` (sync, idempotent); everything else is the
    standard ``QueryBackedViewModel`` machinery.
    """

    def __init__(self, session_factory: Any) -> None:
        super().__init__()
        self._session_factory = session_factory
        self._topic_id: int | None = None
        self._summary: _Summary | None = None

    # ------------------------------------------------------------------
    # Read-only view-side accessors
    # ------------------------------------------------------------------

    @property
    def topic_id(self) -> int | None:
        return self._topic_id

    @property
    def summary(self) -> _Summary | None:
        return self._summary

    # ------------------------------------------------------------------
    # Inputs
    # ------------------------------------------------------------------

    def set_topic_id(self, topic_id: int | None) -> None:
        """Set the cursor topic id and (re)schedule a fetch. Idempotent on the same id.

        ``None`` clears the panel synchronously — there's no DB work to do, so we skip the
        debounce/fetch path and just emit dirty.
        """
        if topic_id == self._topic_id:
            return
        self._topic_id = topic_id
        if topic_id is None:
            self._summary = None
            self.emit(self.dirty)
            return
        self._request_fetch()

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    async def _fetch(self) -> _Summary | None:
        topic_id = self._topic_id
        if topic_id is None:
            return None
        async with self._session_factory() as session:
            topic = await get_topic(session, topic_id)
            if topic is None:
                return _Summary(None, 0, 0, 0, 0)
            subtree_ids = await expand_subtrees(session, [topic_id])
            direct_entries = await count_entries(session, topic_id)
            subtree_entries = await count_entries_filtered(
                session, topic_ids=subtree_ids
            )
            direct_flashcards = await count_flashcards_by_topic(session, topic_id)
            subtree_flashcards = await count_flashcards_by_topics(
                session, subtree_ids
            )
        return _Summary(
            topic=topic,
            direct_entries=direct_entries,
            subtree_entries=subtree_entries,
            direct_flashcards=direct_flashcards,
            subtree_flashcards=subtree_flashcards,
        )

    def _process_fetched_data(self, result: _Summary | None) -> None:
        self._summary = result


# Dim grey for labels; the values themselves render in default foreground so they read as the "data".
_LABEL_STYLE = "rgb(120,120,120)"
_PLACEHOLDER_STYLE = "italic rgb(120,120,120)"


class TopicSummaryView(Vertical):
    """View for ``TopicSummaryViewModel``.

    A vertical stack of ``Static`` lines rendered from the VM's ``summary``. Full repaint on every
    ``dirty`` — the content is tiny (five short lines) so there's no incremental-update advantage to
    chase.

    The description can be multi-line; we render it on its own ``Static`` with markup wrapping so a
    longer description grows the panel vertically rather than overflowing. The other rows are single-
    line. If you embed this somewhere with a strict height budget, the parent CSS should set
    ``overflow: hidden`` or ``height: auto`` as appropriate.
    """

    DEFAULT_CSS = """
    TopicSummaryView {
        height: auto;
        padding: 0 1;
    }
    TopicSummaryView > Static {
        height: auto;
        width: 100%;
    }
    TopicSummaryView > #topic-summary-description {
        padding: 1 0 0 0;
        color: rgb(180,180,180);
    }
    """

    def __init__(self, view_model: TopicSummaryViewModel, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._vm = view_model

    def compose(self):
        yield Static("", id="topic-summary-header")
        yield Static("", id="topic-summary-counts")
        yield Static("", id="topic-summary-description")

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    def _refresh(self) -> None:
        header = self.query_one("#topic-summary-header", Static)
        counts = self.query_one("#topic-summary-counts", Static)
        description = self.query_one("#topic-summary-description", Static)

        summary = self._vm.summary
        if self._vm.is_loading and summary is None:
            header.update(Text("loading…", style=_PLACEHOLDER_STYLE))
            counts.update("")
            description.update("")
            return
        if summary is None or summary.topic is None:
            header.update(Text("no topic highlighted", style=_PLACEHOLDER_STYLE))
            counts.update("")
            description.update("")
            return

        topic = summary.topic
        header_text = Text()
        header_text.append(topic.name, style="bold")
        header_text.append(f"  [{topic.id}]", style=_LABEL_STYLE)
        header.update(header_text)

        counts_text = Text()
        counts_text.append("entries: ", style=_LABEL_STYLE)
        counts_text.append(str(summary.direct_entries))
        counts_text.append(f" ({summary.subtree_entries} in subtree)", style=_LABEL_STYLE)
        counts_text.append("\n")
        counts_text.append("flashcards: ", style=_LABEL_STYLE)
        counts_text.append(str(summary.direct_flashcards))
        counts_text.append(f" ({summary.subtree_flashcards} in subtree)", style=_LABEL_STYLE)
        counts.update(counts_text)

        if topic.description:
            description.update(topic.description)
        else:
            description.update(Text("(no description)", style=_PLACEHOLDER_STYLE))
