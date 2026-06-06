"""``ResourcePreview`` — read-only name + description for a surface's highlighted resource.

A pure auxiliary view bound to whichever surface VM drives it — the loader (top preview) or the
linker (bottom preview). It reads that VM's ``cursor_target`` and renders the resource's name and
summary; a ``ResourceSection`` (only reachable through the loader) carries just a title.

Subscribes to both ``cursor_changed`` (the highlight moved) and ``dirty`` (the data behind the
cursor changed — e.g. the linker's pool reloaded under a fixed cursor index, where ``cursor_target``
is recomputed without a fresh highlight). Holds no state and is never its VM's ``ViewBase`` owner, so
it wires its subscriptions manually, mirroring the status view.

A ``VerticalScroll`` so a long summary scrolls past the box cap rather than clipping; non-focusable
(readonly auxiliary, kept out of the keyboard focus graph — scroll via mouse wheel). Which of the two
previews is *visible* is a focus concern owned by ``ResourceViewer``.
"""

from __future__ import annotations

from typing import Any

from rich.style import Style
from rich.text import Text

from textual.containers import VerticalScroll
from textual.widgets import Static

from rhizome.app.resource_viewer.linker import ResourceLinkerModel
from rhizome.app.resource_viewer.loader import ResourceLoaderModel

_NAME_STYLE = Style(color="rgb(210,210,210)", bold=True)
_BODY_STYLE = Style(color="rgb(150,150,150)")
_EMPTY_STYLE = Style(color="rgb(95,95,95)", italic=True)


class ResourcePreview(VerticalScroll, can_focus=False):
    """Read-only, scrollable preview of a surface's highlighted resource. See module docstring."""

    DEFAULT_CSS = """
    ResourcePreview {
        background: transparent;
    }
    ResourcePreview > Static {
        width: 1fr;
        height: auto;
        background: transparent;
    }
    """

    def __init__(self, view_model: ResourceLoaderModel | ResourceLinkerModel, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._vm = view_model
        self._body = Static()

    def compose(self):
        yield self._body

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.cursor_changed, self._refresh)
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.cursor_changed, self._refresh)
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    def _refresh(self) -> None:
        self._body.update(self._render_target(self._vm.cursor_target))

    def _render_target(self, target: Any) -> Text:
        if target is None:
            return Text("— nothing selected —", style=_EMPTY_STYLE)
        # A ``Resource`` carries ``name`` + ``summary``; a ``ResourceSection`` carries only ``title``.
        name = getattr(target, "name", None) or getattr(target, "title", "") or "—"
        summary = getattr(target, "summary", None)
        out = Text(name, style=_NAME_STYLE)
        out.append("\n\n")
        out.append(summary, style=_BODY_STYLE) if summary else out.append(
            "No description.", style=_EMPTY_STYLE
        )
        return out
