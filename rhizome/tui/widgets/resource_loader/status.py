"""``ResourceStatus`` — the loader's per-axis load summary.

A non-focusable ``Static`` over ``ResourceLoaderModel``: a header line with the resource count (the
current filter set against the library total) followed by one line per axis (index / global / local),
each showing how many resources, sections, and (approximate) tokens that axis carries. The bracketed,
axis-coloured letter doubles as the count label and the glyph legend (``3 [I]ndexed``), matching the
``[IGL]`` glyphs in the tree. Repaints on any load or data change.
"""

from __future__ import annotations

from typing import Any

from rich.style import Style
from rich.text import Text

from textual.widgets import Static

from rhizome.app.resource_loader import AxisStats, ResourceLoaderModel

_COUNT   = Style(color="rgb(220,220,220)")
_DIM     = Style(color="rgb(110,110,110)")
_BRACKET = Style(color="rgb(90,90,90)")
_INDEX   = Style(color="rgb(120,210,110)")   # green
_GLOBAL  = Style(color="rgb(235,180,90)")    # yellow
_LOCAL   = Style(color="rgb(235,140,60)")    # orange


def _fmt_tokens(n: int) -> str:
    """Approximate token weight as ``~5.2k`` / ``~800`` — the ``~`` flags it as an estimate."""
    return f"~{n / 1000:.1f}k" if n >= 1000 else f"~{n}"


class ResourceStatus(Static):
    """Two-line load summary + glyph legend. See module docstring."""

    DEFAULT_CSS = """
    ResourceStatus {
        height: auto;
        background: transparent;
    }
    """

    def __init__(self, view_model: ResourceLoaderModel, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._vm = view_model

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.Callbacks.OnDataChanged,      self._refresh)
        self._vm.subscribe(self._vm.Callbacks.OnLoadStateChanged, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.Callbacks.OnDataChanged,      self._refresh)
        self._vm.unsubscribe(self._vm.Callbacks.OnLoadStateChanged, self._refresh)

    # ``OnLoadStateChanged`` carries a resource id (or None); ``_refresh`` ignores it and repaints
    # wholesale — the status is an aggregate, so any change is a full recount.
    def _refresh(self, *_: Any) -> None:
        stats = self._vm.stats()
        lines = [
            self._resources_line(stats.total_resources, stats.visible_resources),
            self._axis_line("I", "ndexed", _INDEX, stats.indexed),
            self._axis_line("G", "lobal context", _GLOBAL, stats.context_global),
            self._axis_line("L", "ocal context", _LOCAL, stats.context_local),
        ]
        self.update(Text("\n").join(lines))

    def _resources_line(self, total: int, visible: int) -> Text:
        # When a search / topic filter is active, lead with the filter-set count: "3 of 12 resources".
        text = Text()
        if visible != total:
            text.append(f"{visible}", _COUNT)
            text.append(" of ", _DIM)
        text.append(f"{total}", _COUNT)
        text.append(" resources", _DIM)
        return text

    def _axis_line(self, letter: str, rest: str, colour: Style, stats: AxisStats) -> Text:
        # The bracketed axis letter doubles as count label and glyph legend, e.g. "3 [I]ndexed".
        text = Text()
        text.append(f"{stats.resources}", _COUNT)
        text.append(" ", _DIM)
        text.append("[", _BRACKET)
        text.append(letter, colour)
        text.append("]", _BRACKET)
        text.append(rest, _DIM)
        self._append_footprint(text, stats)
        return text

    def _append_footprint(self, text: Text, stats: AxisStats) -> None:
        text.append(" · ", _DIM)
        text.append(f"{stats.sections}", _COUNT)
        text.append(" sections", _DIM)
        text.append(" · ", _DIM)
        text.append(_fmt_tokens(stats.tokens), _COUNT)
        text.append(" tokens", _DIM)
