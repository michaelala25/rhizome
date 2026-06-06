"""Two-axis filter dialog: entry-type multi-select over a mutually-exclusive flashcard-presence
radio.

Layout: ``Vertical`` with three rows — type row, flashcard row (a ``Horizontal`` containing the
radios Static + the compact ``_OneOfInput`` + a close-bracket Static), and hint row. Cursor model
is two-axis (``_row`` / ``_col``); the flashcard row has one extra column past the three real
radios for the "One of" pseudo-option.

"One of" sub-flow: ``space`` on it sets ``_one_of_selected`` view-side, clears the VM filter (the
two axes are mutually exclusive at the dialog layer), and focuses the input. ``enter`` parses the
buffer via ``_parse_id_list`` and pushes the tuple via ``vm.set_flashcard_ids_filter``; empty
parsed result clears the filter but keeps the mode. ``escape`` clears the buffer, drops the
``_one_of_selected`` flag, and wipes both VM axes. Switching to a real radio preserves the buffer
but flips the flag off.

Keys: ``↑``/``↓`` row · ``←``/``→`` within row (wrap) · ``space`` toggle · ``r`` clears both axes
and the One-of buffer · ``f``/``escape`` dismiss · ``s``/``e`` swap. Both rows push to the VM
immediately on toggle — no separate "apply".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, Static

from rhizome.db.models import EntryType

from rhizome.app.browser.tabs.entries.tab import EntryTabModel
from rhizome.tui.keybindings import Keybind

if TYPE_CHECKING:
    from .view import EntryTab


# Entry-type filter options (row 0). Mirrors ``EntryType`` enum order.
_TYPE_OPTIONS: tuple[EntryType, ...] = tuple(EntryType)

# Flashcard-presence radio options (row 1). Maps label → ``vm.has_flashcards`` value.
_FLASHCARD_OPTIONS: tuple[tuple[str, bool | None], ...] = (
    ("None", None),
    ("Any", True),
    ("No flashcards", False),
)

# Fourth flashcard-filter option label. Backed by ``vm.flashcard_ids`` (not ``has_flashcards``).
_ONE_OF_LABEL = "One of:"

# Lead-in width — wide enough for "filter by flashcards:" + 2-space gutter so option columns line
# up vertically across rows.
_FILTER_LEAD_WIDTH = len("filter by flashcards:") + 2


class _OneOfInput(Input):
    """Compact one-row input for the "One of:" radio. Custom escape binding routes through the
    parent dialog so escape clears+exits without Input's default swallowing the keystroke. The
    placeholder doubles as the format hint."""

    BINDINGS = [
        Keybind.BrowserFilterCloseSubarea.as_binding("handle_escape", show=False),
    ]

    def __init__(self, dialog: "FilterMenu", **kwargs: Any) -> None:
        super().__init__(
            compact=True,
            placeholder="comma-separated ids",
            **kwargs,
        )
        self._dialog = dialog

    def action_handle_escape(self) -> None:
        self._dialog.cancel_one_of()


class FilterMenu(Vertical, can_focus=True):
    """See the module docstring for layout, the "One of" sub-flow, and the keybindings."""

    BINDINGS = [
        Keybind.CursorUp.   as_binding("cursor_up",    show=False),
        Keybind.CursorDown. as_binding("cursor_down",  show=False),
        Keybind.CursorLeft. as_binding("cursor_left",  show=False),
        Keybind.CursorRight.as_binding("cursor_right", show=False),
        Keybind.Toggle.     as_binding("toggle",       show=False),
        Keybind.CloseMenu.  as_binding("cancel",       show=True),
        Keybind.MenuReset.  as_binding("reset",        show=True),

        # TODO: these really shouldn't live here at all, they should just bubble up to the EntryTab and
        # be handled there.
        Keybind.BrowserSort  .as_binding("swap_to('sort')", show=False),
        Keybind.BrowserFilter.as_binding("cancel",          show=False),
        Keybind.BrowserEdit  .as_binding("swap_to('edit')", show=False)
    ]

    def __init__(
        self,
        view_model: EntryTabModel,
        tab: "EntryTab",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model
        self._tab = tab
        # ``_row`` ∈ {0, 1}; ``_col`` clamped to the active row's length on row change. The
        # flashcards row has one extra column past the real radios for the "One of" pseudo-option.
        self._row: int = 0
        self._col: int = 0
        # "One of" view-side state. The buffer persists across radio switches and dialog
        # open/close cycles so the user can return to it later.
        self._one_of_selected: bool = False
        self._one_of_buffer: str = ""

    def compose(self):
        yield Static(id="type-row")
        with Horizontal(id="flashcard-row"):
            yield Static(id="flashcard-radios")
            yield _OneOfInput(self, id="one-of-input")
            yield Static(id="one-of-close-bracket")
        yield Static(id="hint-row")

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        # Input starts disabled — "One of" isn't selected by default.
        self.query_one("#one-of-input", _OneOfInput).disabled = True
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    def on_focus(self) -> None:
        self.call_after_refresh(self._refresh)

    def on_blur(self) -> None:
        self.call_after_refresh(self._refresh)

    def prepare_for_show(self) -> None:
        # Active selections (VM filters + view-side _one_of_*) persist across opens by design.
        self._row = 0
        self._col = 0

    def _row_lengths(self) -> tuple[int, int]:
        return (len(_TYPE_OPTIONS), len(_FLASHCARD_OPTIONS) + 1)

    def _one_of_col(self) -> int:
        return len(_FLASHCARD_OPTIONS)

    def _refresh(self) -> None:
        try:
            type_row = self.query_one("#type-row", Static)
            radios = self.query_one("#flashcard-radios", Static)
            close_bracket = self.query_one("#one-of-close-bracket", Static)
            hint = self.query_one("#hint-row", Static)
            input_widget = self.query_one("#one-of-input", _OneOfInput)
        except Exception:
            return
        cursor_color = "bold #ffd700" if self.has_focus else "bold #6a6a6a"
        type_row.update(self._render_type_row(cursor_color))
        radios.update(self._render_flashcard_radios(cursor_color))
        close_bracket.update(self._render_close_bracket())
        hint.update(self._render_hint())
        # Sync Input enabled/value. Skip the value-mirror under focus — don't clobber typing.
        input_widget.disabled = not self._one_of_selected
        if not input_widget.has_focus and input_widget.value != self._one_of_buffer:
            input_widget.value = self._one_of_buffer

    def _selected_types(self) -> set[EntryType]:
        if self._vm.entry_types is None:
            return set(_TYPE_OPTIONS)
        return set(self._vm.entry_types)

    def _type_filter_active(self) -> bool:
        return self._selected_types() != set(_TYPE_OPTIONS)

    def _flashcard_filter_active(self) -> bool:
        # ``_one_of_selected`` covers the case where the user has chosen "One of" but not yet
        # submitted a buffer (both VM axes still None) — we still want the row header active.
        return (
            self._one_of_selected
            or self._vm.has_flashcards is not None
            or self._vm.flashcard_ids is not None
        )

    def _render_type_row(self, cursor_color: str) -> Text:
        text = Text()
        selected = self._selected_types()
        lead_style = "bold #5fd75f" if self._type_filter_active() else "dim"
        text.append(_pad_lead("filter by type:"), style=lead_style)
        for i, opt in enumerate(_TYPE_OPTIONS):
            is_cursor = self._row == 0 and self._col == i
            is_sel = opt in selected
            marker = "[x]" if is_sel else "[ ]"
            marker_style = "#5fd75f" if is_sel else "#787878"
            label_style = cursor_color if is_cursor else ""
            text.append(marker, style=marker_style)
            text.append(" ")
            text.append(opt.value, style=label_style)
            if i < len(_TYPE_OPTIONS) - 1:
                text.append("    ")
        return text

    def _render_flashcard_radios(self, cursor_color: str) -> Text:
        """Renders text up through the "One of:" label + trailing "["; the Input + close-bracket
        are sibling widgets laid out by the parent Horizontal."""
        text = Text()
        active = self._vm.has_flashcards
        lead_style = "bold #5fd75f" if self._flashcard_filter_active() else "dim"
        text.append(_pad_lead("filter by flashcards:"), style=lead_style)
        for i, (label, value) in enumerate(_FLASHCARD_OPTIONS):
            is_cursor = self._row == 1 and self._col == i
            # When "One of" is active the bullet jumps to the pseudo-option — explicitly suppress
            # the VM-derived bullet, since ``has_flashcards=None`` is also a legitimate VM state.
            is_sel = (not self._one_of_selected) and value == active
            marker = "(•)" if is_sel else "( )"
            marker_style = "#5fd75f" if is_sel and value is not None else (
                "#787878" if not is_sel else ""
            )
            label_style = cursor_color if is_cursor else ""
            text.append(marker, style=marker_style)
            text.append(" ")
            text.append(label, style=label_style)
            text.append("    ")
        # "One of:" pseudo-option. Trailing "[" demarcates the start of the input field.
        is_cursor = self._row == 1 and self._col == self._one_of_col()
        marker = "(•)" if self._one_of_selected else "( )"
        marker_style = "#5fd75f" if self._one_of_selected else "#787878"
        label_style = cursor_color if is_cursor else ""
        bracket_style = "" if self._one_of_selected else "#5a5a5a"
        text.append(marker, style=marker_style)
        text.append(" ")
        text.append(_ONE_OF_LABEL, style=label_style)
        text.append(" ")
        text.append("[", style=bracket_style)
        return text

    def _render_close_bracket(self) -> Text:
        bracket_style = "" if self._one_of_selected else "#5a5a5a"
        return Text("]", style=bracket_style)

    def _render_hint(self) -> Text:
        # Extended with the selection-clearing warning while multi-select is on (same pattern as
        # ``EntriesSortMenu._extra_hint``).
        hint = Text()
        hint.append(
            "↑/↓ row • ←/→ move • space toggle • r reset • f/esc dismiss",
            style="dim",
        )
        if self._vm.multi_select_active:
            hint.append("   ", style="dim")
            hint.append("Toggling clears your selection.", style="#ff8787")
        return hint

    def action_cursor_up(self) -> None:
        self._row = (self._row - 1) % 2
        self._col = min(self._col, self._row_lengths()[self._row] - 1)
        self._refresh()

    def action_cursor_down(self) -> None:
        self._row = (self._row + 1) % 2
        self._col = min(self._col, self._row_lengths()[self._row] - 1)
        self._refresh()

    def action_cursor_left(self) -> None:
        n = self._row_lengths()[self._row]
        self._col = (self._col - 1) % n
        self._refresh()

    def action_cursor_right(self) -> None:
        n = self._row_lengths()[self._row]
        self._col = (self._col + 1) % n
        self._refresh()

    def action_toggle(self) -> None:
        # Row 0: multi-toggle on the cursor's type; ``set_type_filter`` collapses all-selected → None.
        # Row 1: radio-select on the cursor's option, or activate the "One of" pseudo-option.
        if self._row == 0:
            target = _TYPE_OPTIONS[self._col]
            selected = self._selected_types()
            if target in selected:
                selected.discard(target)
            else:
                selected.add(target)
            if selected == set(_TYPE_OPTIONS):
                self._vm.set_type_filter(None)
            else:
                # Preserve enum order so the kwargs snapshot is stable across toggles.
                self._vm.set_type_filter(tuple(t for t in _TYPE_OPTIONS if t in selected))
            return
        if self._col == self._one_of_col():
            self._activate_one_of()
        else:
            _, value = _FLASHCARD_OPTIONS[self._col]
            self._one_of_selected = False
            self._vm.set_flashcard_filter(value)

    def _activate_one_of(self) -> None:
        # Idempotent on the flag — also used to re-focus the input on subsequent ``space`` presses.
        if not self._one_of_selected:
            self._one_of_selected = True
            # Clear the VM filter — the two axes are mutually exclusive at the display layer.
            self._vm.set_flashcard_filter(None)
        self._refresh()
        input_widget = self.query_one("#one-of-input", _OneOfInput)
        input_widget.disabled = False
        input_widget.focus()

    def submit_one_of(self, value: str) -> None:
        """``enter`` inside the input: parse via ``_parse_id_list`` and push to the VM. Empty
        parsed result clears the filter but stays in "One of" mode — submitting nothing reads as
        "stop filtering", not "no rows match"."""
        self._one_of_buffer = value
        ids = _parse_id_list(value)
        if ids:
            self._vm.set_flashcard_ids_filter(ids)
        else:
            self._vm.set_flashcard_ids_filter(None)
        self._refresh()
        self.focus()

    def cancel_one_of(self) -> None:
        """``escape`` inside the input: clear the buffer + flag, drop both VM axes, return focus."""
        self._one_of_buffer = ""
        self._one_of_selected = False
        self._vm.set_flashcard_filter(None)
        self._refresh()
        self.focus()

    @on(Input.Submitted)
    def _on_input_submitted(self, event: Input.Submitted) -> None:
        # _OneOfInput is the only Input under this dialog — guard against future re-parenting.
        if isinstance(event.input, _OneOfInput):
            self.submit_one_of(event.value)

    def action_reset(self) -> None:
        # ``set_flashcard_filter(None)`` wipes both axes thanks to the VM's mutual-exclusion
        # invariant, so no separate call to clear the ids axis is needed.
        self._vm.set_type_filter(None)
        self._vm.set_flashcard_filter(None)
        self._one_of_selected = False
        self._one_of_buffer = ""
        self._row = 0
        self._col = 0
        self._refresh()

    def action_cancel(self) -> None:
        self._tab.hide_dialog()

    def action_swap_to(self, name: str) -> None:
        self._tab.toggle_dialog(name)  # type: ignore[arg-type]


def _pad_lead(label: str) -> str:
    return label.ljust(_FILTER_LEAD_WIDTH)


def _parse_id_list(text: str) -> tuple[int, ...]:
    """Parse a comma-separated id list. Tokens that don't parse as ``int`` (or are empty after
    strip) are dropped silently — a stray comma or partial edit doesn't reject the submission."""
    out: list[int] = []
    for token in text.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            out.append(int(token))
        except ValueError:
            continue
    return tuple(out)
