"""SearchInput — generic search-box widget over any ``SearchableViewModelMixin`` VM.

Visually a tight 3-row box with a transparent background and a ``#3a3a3a`` border that flips
accent when focus is anywhere inside (``:focus-within``); the keybinding hint rides the top
border on the right (``border_title`` + ``border_title_align = "right"``). A small clickable
``[×]`` sits at the right edge of the content row as a mouse-driven shortcut for clear.

Layout
------
``SearchInput`` is a ``Horizontal`` container — the chrome — composing two children:

  * ``_SearchField`` (an ``Input`` subclass, rendered borderless via ``compact=True``) — the
    actual text buffer; takes ``1fr`` of the content row.
  * ``_ClearButton`` (a small ``Static``) — 3 cells wide, click bubbles to the container.

The caller-supplied ``id`` is forwarded to the inner ``_SearchField`` rather than the outer
chrome, so existing ``query_one('#search-input').focus()`` and ``focused.id`` checks in parent
widgets keep working transparently — they target the focus-receiving widget, which is the
inner field.

Input behaviour
---------------
  * ``enter`` — submit the current buffer to ``vm.set_search``.
  * ``esc`` × 2 *in rapid succession* (≤``_RAPID_ESCAPE_WINDOW`` seconds apart) — clear the
    buffer and submit the empty query. A single ``esc``, or two ``esc``\\s separated by more
    than the window, do nothing — the gesture is intentionally invisible (no border-title
    feedback). One ``esc`` was previously enough to "arm" a clear, but that conflicted with
    using ``esc`` to drop focus elsewhere — pairing the trigger to a deliberate double-tap
    makes the gesture unambiguous.
  * **click on ``[×]``** — same effect as the double-escape clear.

Generic on the VM
-----------------
``SearchInput`` is generic on the VM type, bound to ``SearchableViewModelMixin``. Concrete
widget instances are typically constructed with the bound spelled out (e.g.
``SearchInput[KnowledgeEntryBrowserTabViewModel]``) so the type-checker can keep the VM-typed
``self._vm`` attribute accurate. At runtime the widget only ever calls ``vm.set_search`` —
nothing VM-specific leaks across the boundary.
"""

from __future__ import annotations

from time import monotonic
from typing import Any, Generic, TypeVar

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal
from textual.events import Click
from textual.widgets import Input, Static

from .view_model_mixin import SearchableViewModelMixin

VM = TypeVar("VM", bound=SearchableViewModelMixin)

# Rapid double-escape window — the second ``esc`` must land within this many seconds of the
# first to trigger a clear. Tight enough that a single deliberate ``esc`` (e.g. dropping focus
# elsewhere) doesn't accidentally arm the gesture; loose enough to absorb a natural double-tap
# rhythm.
_RAPID_ESCAPE_WINDOW: float = 0.5


class _SearchField(Input):
    """Inner Input field. Owns the text buffer; ``escape`` taps are deferred to the parent
    ``SearchInput`` so the rapid-double-tap gesture state lives in one place. Rendered
    borderless inside the parent's bordered chrome via ``compact=True``."""

    BINDINGS = [
        Binding("escape", "handle_escape", show=False),
    ]

    # ``background: transparent`` overrides Input's default ``$surface`` so the inner field
    # reads as part of the parent chrome's background. The remaining sizing (borderless,
    # ``height: 1``, ``padding: 0``) comes from ``compact=True`` — which sets
    # ``border: none !important`` on the ``-textual-compact`` class, beating Input's own
    # ``border: tall`` baked into ``Input { ... }``.
    DEFAULT_CSS = """
    _SearchField {
        background: transparent;
        width: 1fr;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(compact=True, **kwargs)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Submit handler lives on the inner Input so the parent's ``on_input_submitted`` slot
        # stays free for siblings (the inner field is the only Input under SearchInput, but
        # routing through the field keeps the wiring explicit).
        if event.input is not self:
            return
        parent = self.parent
        assert isinstance(parent, SearchInput)
        parent._vm.set_search(event.value)

    def action_handle_escape(self) -> None:
        parent = self.parent
        assert isinstance(parent, SearchInput)
        parent._handle_escape_tap()


class _ClearButton(Static):
    """Clickable ``[×]`` mounted at the right of the search bar. Non-focusable; clicking
    bubbles a ``Click`` event up to ``SearchInput.on_click``, where it's filtered on widget
    identity and routed into ``clear()``."""

    can_focus = False

    DEFAULT_CSS = """
    _ClearButton {
        width: 3;
        height: 1;
        content-align: center middle;
        color: #707070;
        margin: 0 1 0 0;
    }
    _ClearButton:hover {
        color: #ff8787;
        text-style: bold;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        # ``Text(...)`` rather than a raw string so Rich treats ``[×]`` literally instead of
        # parsing the brackets as markup tags.
        super().__init__(Text("[×]"), **kwargs)


class SearchInput(Horizontal, Generic[VM]):
    DEFAULT_CSS = """
    SearchInput {
        background: transparent;
        border: solid #3a3a3a;
        height: 3;
        layout: horizontal;
    }
    SearchInput:focus-within {
        border: solid $accent;
    }
    """

    def __init__(
        self,
        view_model: VM,
        **kwargs: Any,
    ) -> None:
        # Forward the caller-supplied id to the inner field rather than the outer chrome.
        # Callers focus the search-input through ``query_one('#...').focus()`` and identify
        # the focused region via ``focused.id``; both target the focus-receiving widget,
        # which is the inner ``_SearchField``. The wrapper is invisible to those lookups.
        self._field_id: str | None = kwargs.pop("id", None)
        super().__init__(**kwargs)
        self._vm = view_model
        # Monotonic timestamp of the most recent escape press, or ``None`` if no recent press
        # is within the rapid-double-tap window. Lives on the container so both the inner
        # field's escape binding and any future entry point (a chord, a hotkey on the chrome)
        # share one gesture state.
        self._last_escape_at: float | None = None
        self.border_title_align = "right"
        self.border_title = "[dim]enter to submit • esc 2x to clear[/]"

    def compose(self):
        field_kwargs: dict[str, Any] = {}
        if self._field_id is not None:
            field_kwargs["id"] = self._field_id
        yield _SearchField(**field_kwargs)
        yield _ClearButton()

    def clear(self) -> None:
        """Blank the buffer and submit the empty query — the canonical "no filter" state.
        Refocuses the inner field so a mouse-driven clear (clicking ``[×]``) leaves the user
        ready to type a new query without a follow-up click."""
        field = self.query_one(_SearchField)
        field.value = ""
        self._vm.set_search("")
        field.focus()

    def _handle_escape_tap(self) -> None:
        """Called by the inner field on each escape press. Two taps within
        ``_RAPID_ESCAPE_WINDOW`` seconds trigger a clear; an isolated tap (or a tap after the
        window has elapsed) just records itself as a possible start of a new double-tap. No
        visible state change either way — the gesture is intentionally invisible per the
        widget's UX contract."""
        now = monotonic()
        prev = self._last_escape_at
        if prev is not None and (now - prev) < _RAPID_ESCAPE_WINDOW:
            self._last_escape_at = None
            self.clear()
        else:
            self._last_escape_at = now

    def on_click(self, event: Click) -> None:
        # The clear button is non-focusable; its click bubbles up to the container. Filtering
        # on widget identity keeps the clear path centralised here. Clicks landing on the
        # inner Input bubble too but aren't stopped, so Textual's own focus-on-click handling
        # still wires up correctly.
        if isinstance(event.widget, _ClearButton):
            event.stop()
            self.clear()
