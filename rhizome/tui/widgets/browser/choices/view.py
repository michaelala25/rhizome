"""ChoiceList — shared base for browser-tab dialogs that present a navigable list of named
choices (Accept/Cancel, Confirm/Cancel, edit-action picker, etc.).

The widget owns the cursor and the navigation/dispatch wiring; subclasses customise rendering
via three optional hooks (``_render_header``, ``_render_lead``, ``_render_hint``,
``_render_choice``) and declare their action handlers via the ``CHOICES`` class attr (or the
``choices()`` method for dynamic option lists).

CHOICES contract
----------------
``CHOICES: dict[str, str]`` maps each label to the name of an action method on the same
class. On ``enter``, the widget looks up the cursor's label, resolves the action name via
``getattr(self, action_name)``, and invokes it. The action method takes no arguments; it can
be sync or async. This mirrors Textual's own ``BINDINGS`` action-string convention.

For widgets whose label set is dynamic (e.g. an edit picker whose options depend on
multi-select state), override ``choices()`` to return the dict; the default reads from the
class-level ``CHOICES``.

Sibling-dialog swap keys (``s`` / ``f`` / ``e`` / ``d``) are *not* bound here. They bubble to
the parent tab's BINDINGS, which owns the dialog mutex — matches the convention established
by ``SortDialog``.
"""

from __future__ import annotations

import inspect
from typing import Any, ClassVar, Generic, Literal, TypeVar

from rich.text import Text
from textual.binding import Binding
from textual.widgets import Static

from ...view_model_base import ViewModelBase

VM = TypeVar("VM", bound=ViewModelBase)


class ChoiceList(Static, Generic[VM], can_focus=True):
    # Subclass declares ``{label: action_method_name}``. Static for fixed choice lists; override
    # ``choices()`` to compute dynamically.
    CHOICES: ClassVar[dict[str, str]] = {}
    # ``"horizontal"`` separates choices with three spaces on a single line;
    # ``"vertical"`` stacks them across newlines.
    ORIENTATION: ClassVar[Literal["horizontal", "vertical"]] = "horizontal"
    # Static inline prefix before the first choice (rendered ``dim``). Override
    # ``_render_lead()`` for dynamic / state-driven content instead.
    LEAD: ClassVar[str | None] = None
    # Static hint line under the choices (rendered ``dim``). Override ``_render_hint()`` for
    # dynamic / state-driven content instead.
    HINT: ClassVar[str | None] = None

    BINDINGS = [
        Binding("left", "cursor_left", show=False),
        Binding("right", "cursor_right", show=False),
        Binding("up", "cursor_up", show=False),
        Binding("down", "cursor_down", show=False),
        Binding("enter", "confirm", show=False),
        Binding("escape", "cancel", show=False),
    ]

    def __init__(self, view_model: VM, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._vm: VM = view_model
        self._cursor: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    def on_focus(self) -> None:
        # Cursor brightness tracks focus — re-render. Can't use a CSS ``:focus`` rule because
        # the per-segment Rich styles in ``_render_choice`` carry their own colour that would
        # override widget-level fg.
        self.call_after_refresh(self._refresh)

    def on_blur(self) -> None:
        self.call_after_refresh(self._refresh)

    def prepare_for_show(self) -> None:
        """Reset the cursor to the first choice. Called by the parent's dialog orchestrator on
        each show transition so a fresh open starts on a predictable position."""
        self._cursor = 0
        self._refresh()

    # ------------------------------------------------------------------
    # Subclass extension points
    # ------------------------------------------------------------------

    def choices(self) -> dict[str, str]:
        """Return the active label → action-name map. Default returns the class-level
        ``CHOICES``; override to compute dynamically."""
        return self.CHOICES

    def _render_header(self) -> Text | None:
        """Optional line(s) rendered *above* the choices (e.g. scoping prose for a
        destructive confirmation). Default returns ``None``."""
        return None

    def _render_lead(self) -> Text | None:
        """Optional inline prefix rendered immediately before the first choice. Default
        returns ``LEAD`` styled ``dim``; override to compute dynamically."""
        return Text(self.LEAD, style="dim") if self.LEAD is not None else None

    def _render_hint(self) -> Text | None:
        """Optional dim hint line rendered *below* the choices. Default returns ``HINT``
        styled ``dim``; override to compute dynamically."""
        return Text(self.HINT, style="dim") if self.HINT is not None else None

    def _render_choice(self, label: str, selected: bool) -> Text:
        """Render one choice. Default is the standard ``► <bold-label>`` (cursor) / ``  <dim-
        label>`` (other) pattern; override for a different visual (e.g. colour-only without
        a marker)."""
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
        """Dispatch the cursor's choice via ``getattr(self, action_name)()``. Action methods
        can be sync or async; we ``await`` the return if it's awaitable."""
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
        """Default ``escape`` handler — no-op. Subclasses override to dismiss the dialog
        (``self._tab.hide_dialog()``) or call ``vm.cancel()`` etc."""

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        labels = list(self.choices().keys())
        # Clamp the cursor in case ``choices()`` shrank under us (e.g. multi-select toggled on
        # while the cursor was on an option that only exists in single-select).
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
        # ``self.update`` swaps the rendered content. Not named ``_render`` because that's a
        # Textual-internal name on Static (returns the cached Visual); shadowing it would
        # make Textual try to use the ``rich.text.Text`` as a Visual and blow up in
        # ``to_strips``.
        self.update(text)
