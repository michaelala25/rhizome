"""``ResourceStatus`` — compact load summary for the resource viewer's status box.

A pure auxiliary view over ``ResourceLoaderVM``: it subscribes to the VM's ``dirty`` group and
re-renders ``load_stats`` on every load-state change. It holds no state of its own and is never the
VM's ``ViewBase`` owner (the loader tree owns that relationship), so it wires its subscription
manually on mount — mirroring how the tree itself subscribes.

Two lines: a count summary (resources · sections · chunks) and the index/context chunk split, with
an ``awaiting embedding`` tail appended only while embeddings are in flight. Colours match the tree's
load glyphs — green for indexed, amber for context-stuffed.
"""

from __future__ import annotations

from typing import Any

from rich.style import Style
from rich.text import Text

from textual.widgets import Static

from rhizome.app.resource_viewer.loader import LoadStats, ResourceLoaderVM

# Mirrors the loader tree's glyph palette: green = indexed, amber = context. Counts read in a near-
# white; labels and separators sit dim so the numbers carry the eye.
_COUNT_STYLE = Style(color="rgb(195,195,195)")
_LABEL_STYLE = Style(color="rgb(110,110,110)")
_INDEX_STYLE = Style(color="rgb(120,210,110)")
_CONTEXT_STYLE = Style(color="rgb(235,180,90)")
_PENDING_STYLE = Style(color="rgb(150,150,150)")

_SEP = ("  ·  ", _LABEL_STYLE)


class ResourceStatus(Static):
    """Load summary view over ``ResourceLoaderVM``. See module docstring."""

    def __init__(self, view_model: ResourceLoaderVM, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._vm = view_model

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    def _refresh(self) -> None:
        self.update(self._render_stats(self._vm.load_stats()))

    def _render_stats(self, stats: LoadStats) -> Text:
        counts = Text.assemble(
            (f"{stats.loaded_resources}/{stats.total_resources}", _COUNT_STYLE),
            (" resources", _LABEL_STYLE),
            _SEP,
            (str(stats.loaded_sections), _COUNT_STYLE),
            (" sections", _LABEL_STYLE),
            _SEP,
            (str(stats.loaded_chunks), _COUNT_STYLE),
            (" chunks", _LABEL_STYLE),
        )

        split_parts = [
            (str(stats.index_chunks), _INDEX_STYLE),
            (" indexed", _LABEL_STYLE),
            _SEP,
            (str(stats.context_chunks), _CONTEXT_STYLE),
            (" context", _LABEL_STYLE),
        ]
        if stats.awaiting_embedding:
            split_parts += [
                _SEP,
                (f"⟳ {stats.awaiting_embedding}", _PENDING_STYLE),
                (" awaiting", _LABEL_STYLE),
            ]
        split = Text.assemble(*split_parts)

        return Text("\n").join([counts, split])
