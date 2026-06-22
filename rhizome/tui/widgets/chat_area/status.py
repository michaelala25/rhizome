"""StatusBar view — renders the conversation's mode, verbosity, and model from a ``StatusBarModel``.

A fixed two-line strip docked at the bottom of the chat area. Line 1 carries the active mode (left) and
the model name (right); line 2 carries the answer verbosity. Every value is a projection the VM owns — the
view just paints it and repaints on the VM's ``OnDirty`` (wired by ``ViewBase``).
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from rhizome.app.chat_area.status import StatusBarModel
from rhizome.tui.widgets.view_base import ViewBase


_LABEL = "rgb(140,140,140)"
_MODEL = "rgb(90,90,90)"

_MODE_COLORS: dict[str, str] = {
    "learn": "rgb(110,140,240)",
    "review": "rgb(170,90,220)",
}

_VERBOSITY_COLORS: dict[str, str] = {
    "terse": "rgb(120,120,120)",
    "standard": "rgb(255,255,255)",
    "verbose": "rgb(90,210,190)",
    "auto": "rgb(255,80,255)",
}


class StatusBar(ViewBase[StatusBarModel]):

    DEFAULT_CSS = """
    StatusBar {
        height: auto;
        background: rgb(12, 12, 12);
        padding: 0 1 1 1;
        border-top: solid rgb(60, 60, 60);
    }
    """

    def __init__(self, vm: StatusBarModel, **kwargs) -> None:
        super().__init__(vm, **kwargs)
        self._static: Static | None = None

    def on_mount(self) -> None:
        self._static = Static(self._build_text())
        self.mount(self._static)

    def on_resize(self, event) -> None:
        # Right-alignment of the model name depends on the bar's pixel width.
        self._refresh()

    def _refresh(self) -> None:
        if self._static is not None:
            self._static.update(self._build_text())

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------

    def _right_align(self, left: Text, right: Text) -> Text:
        gap = max(self.size.width - len(left.plain) - len(right.plain), 2)
        left.append(" " * gap)
        left.append(right)
        return left

    def _build_text(self) -> Text:
        vm = self._vm

        # -- line 1: mode (left), model name (right) --
        mode_line = Text()
        mode_line.append("mode: ", style=_LABEL)
        mode_line.append(vm.mode, style=_MODE_COLORS.get(vm.mode, ""))

        model_text = Text()
        if vm.model_name:
            model_text.append(vm.model_name, style=_MODEL)
        self._right_align(mode_line, model_text)

        # -- line 2: verbosity (left) --
        verbosity_line = Text()
        verbosity_line.append("verbosity: ", style=_LABEL)
        verbosity_line.append(vm.verbosity, style=_VERBOSITY_COLORS.get(vm.verbosity, ""))

        return Text.assemble(mode_line, "\n", verbosity_line)
