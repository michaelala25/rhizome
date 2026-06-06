"""Reusable base widget for in-pane choice dialogs.

A ``ChoiceList`` renders a list of named choices, owns the cursor + arrow nav + enter/escape,
and dispatches the selected choice to an action method on the subclass.

``CHOICES: dict[label, action_method_name]`` mirrors Textual's ``BINDINGS`` action-string
convention. On ``enter``, the cursor's label is resolved to ``getattr(self, action_name)`` and
invoked (sync or async). Override ``choices()`` for a dynamic option set.

Subclass customisation hooks: ``LEAD`` / ``HINT`` / ``ORIENTATION`` class attrs, or the
``_render_header`` / ``_render_lead`` / ``_render_hint`` / ``_render_choice`` methods for
state-driven content. Override ``action_cancel`` to dismiss the dialog on escape.
"""

from __future__ import annotations

import inspect
from typing import Any, ClassVar, Generic, Literal, TypeVar

from rich.text import Text
from textual.widgets import Static

from rhizome.app.model import ViewModelBase
from rhizome.tui.keybindings import Keybind

VM = TypeVar("VM", bound=ViewModelBase)


class ChoiceList(Static, Generic[VM], can_focus=True):
    """Focusable list of named choices that dispatches to subclass action methods."""

    CHOICES: ClassVar[dict[str, str]] = {}
    ORIENTATION: ClassVar[Literal["horizontal", "vertical"]] = "horizontal"
    # Inline prefix before the first choice, rendered ``dim``.
    LEAD: ClassVar[str | None] = None
    # Hint line below the choices, rendered ``dim``.
    HINT: ClassVar[str | None] = None

    BINDINGS = [
        Keybind.CursorLeft. as_binding("cursor_left",  show=False),
        Keybind.CursorRight.as_binding("cursor_right", show=False),
        Keybind.CursorUp.   as_binding("cursor_up",    show=False),
        Keybind.CursorDown. as_binding("cursor_down",  show=False),
        Keybind.MenuConfirm.as_binding("confirm",      show=False),
        Keybind.CloseMenu.  as_binding("cancel",       show=False),
    ]

    def __init__(self, view_model: VM | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._vm: VM | None = view_model
        self._cursor: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        # VM-less ChoiceLists (purely view-driven action menus) skip the dirty subscription —
        # nothing on a data model can change their rendering. Focus-driven repaints still flow
        # through ``on_focus`` / ``on_blur``.
        if self._vm is not None:
            self._vm.subscribe(self._vm.dirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        if self._vm is not None:
            self._vm.unsubscribe(self._vm.dirty, self._refresh)

    def on_focus(self) -> None:
        # Cursor brightness tracks focus; a CSS ``:focus`` rule wouldn't reach the per-segment
        # Rich styles in ``_render_choice``.
        self.call_after_refresh(self._refresh)

    def on_blur(self) -> None:
        self.call_after_refresh(self._refresh)

    def prepare_for_show(self) -> None:
        """Reset cursor to the first choice. Call from the parent dialog orchestrator on each
        show so a fresh open lands on the most-likely-default action."""
        self._cursor = 0
        self._refresh()

    # ------------------------------------------------------------------
    # Subclass extension points
    # ------------------------------------------------------------------

    def choices(self) -> dict[str, str]:
        """Active label → action-name map. Override for dynamic option sets."""
        return self.CHOICES

    def _render_header(self) -> Text | None:
        """Optional line(s) above the choices (e.g. scoping prose for a destructive confirm)."""
        return None

    def _render_lead(self) -> Text | None:
        return Text(self.LEAD, style="dim") if self.LEAD is not None else None

    def _render_hint(self) -> Text | None:
        return Text(self.HINT, style="dim") if self.HINT is not None else None

    def _render_choice(self, label: str, selected: bool) -> Text:
        """``► bold`` for the cursor, ``  dim`` for others. Override for a different visual."""
        cursor_color = "bold #ffd700" if self.has_focus else "bold #6a6a6a"
        text = Text()
        if selected:
            text.append("► ", style=cursor_color)
            text.append(label, style="bold")
        else:
            text.append("  ")
            text.append(label, style="dim")
        return text

    # ------------------------------------------------------------------
    # Bindings
    # ------------------------------------------------------------------

    def action_cursor_left(self) -> None:
        self._move(-1)

    def action_cursor_right(self) -> None:
        self._move(1)

    def action_cursor_up(self) -> None:
        self._move(-1)

    def action_cursor_down(self) -> None:
        self._move(1)

    def _move(self, delta: int) -> None:
        n = len(self.choices())
        if n == 0:
            return
        self._cursor = (self._cursor + delta) % n
        self._refresh()

    async def action_confirm(self) -> None:
        """Resolve and invoke the cursor's action method (sync or async)."""
        labels = list(self.choices().keys())
        if not labels or self._cursor >= len(labels):
            return
        action_name = self.choices()[labels[self._cursor]]
        method = getattr(self, action_name, None)
        if method is None:
            return
        result = method()
        if inspect.isawaitable(result):
            await result

    def action_cancel(self) -> None:
        """Default ``escape`` handler — no-op. Subclasses override to dismiss the dialog."""

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        labels = list(self.choices().keys())
        # Clamp in case ``choices()`` shrank under us (dynamic option sets).
        if labels and self._cursor >= len(labels):
            self._cursor = len(labels) - 1

        text = Text()
        header = self._render_header()
        if header is not None:
            text.append(header)
            text.append("\n")
        lead = self._render_lead()
        if lead is not None:
            text.append(lead)
        sep = "   " if self.ORIENTATION == "horizontal" else "\n"
        for i, label in enumerate(labels):
            text.append(self._render_choice(label, i == self._cursor))
            if i < len(labels) - 1:
                text.append(sep)
        hint = self._render_hint()
        if hint is not None:
            text.append("\n")
            text.append(hint)
        # Not named ``_render`` — that's a Textual-internal hook on ``Static`` returning the
        # cached Visual; shadowing it would crash ``to_strips`` with our ``rich.text.Text``.
        self.update(text)
