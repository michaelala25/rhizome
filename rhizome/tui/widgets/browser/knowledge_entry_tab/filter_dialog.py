"""Two-axis filter picker (entry-type + flashcard-presence). Sits in the same screen slot as the
other dialogs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, Static

from rhizome.db.models import EntryType

from .view_model import KnowledgeEntryBrowserTabViewModel

if TYPE_CHECKING:
    from .view import KnowledgeEntryBrowserTabView


# Entry-type filter options (first filter row). Mirrors ``EntryType`` enum order.
_TYPE_OPTIONS: tuple[EntryType, ...] = tuple(EntryType)

# Flashcard-presence filter options (second filter row). Mutually exclusive — exactly one is the
# active value at any time. ``None`` is the "no filter active" baseline (default). Label / value
# pairs:
#   * "None"           → has_flashcards=None   (no filter applied — default)
#   * "Any"            → has_flashcards=True   (entries with at least one linked flashcard)
#   * "No flashcards"  → has_flashcards=False  (entries with none)
_FLASHCARD_OPTIONS: tuple[tuple[str, bool | None], ...] = (
    ("None", None),
    ("Any", True),
    ("No flashcards", False),
)

# Label for the fourth flashcard-filter option: "One of: <buffer>". The option is view-side only
# for now — when active, the radio bullet is on this label and a compact Input next to it accepts
# the buffer. Selecting any of the other three radios disables "One of" but preserves the buffer
# so the user can return to it. The VM is untouched by this option at present.
_ONE_OF_LABEL = "One of:"

# Lead-in column width for the two filter-row labels — wide enough for the longer of the two
# ("filter by flashcards:") with a 2-space gutter, so the option columns line up vertically.
_FILTER_LEAD_WIDTH = len("filter by flashcards:") + 2


class _OneOfInput(Input):
    """Compact single-row input mounted next to the "One of:" radio. Adds an escape binding that
    routes back to the parent dialog so escape clears+exits without Textual's default Input
    behaviour swallowing the keystroke.

    The placeholder also serves as the format hint — comma-separated integer ids."""

    BINDINGS = [
        Binding("escape", "handle_escape", show=False),
    ]

    def __init__(self, dialog: "_FilterDialog", **kwargs: Any) -> None:
        super().__init__(
            compact=True,
            placeholder="comma-separated ids",
            **kwargs,
        )
        self._dialog = dialog

    def action_handle_escape(self) -> None:
        self._dialog.cancel_one_of()


class _FilterDialog(Vertical, can_focus=True):
    """Layout: three children stacked vertically.
      * Row 0 — ``filter by type:`` (multi-select checkboxes mirroring ``EntryType``). Selection
        state derives directly from ``vm.entry_types`` (``None`` = all types selected).
      * Row 1 — ``filter by flashcards:`` (mutually-exclusive radio: None / Any / No flashcards /
        One of: <input>). The first three derive from ``vm.has_flashcards``; the fourth lives
        view-side as ``_one_of_selected`` + ``_one_of_buffer`` and isn't yet wired to the VM.
      * Row 2 — hint line.

    "One of:" semantics: pressing ``space`` on it activates the radio (clears any prior VM
    flashcard filter, since the two are mutually exclusive) and focuses the compact Input. Inside
    the Input, ``enter`` submits (buffer + selection preserved, focus back to dialog) and
    ``escape`` cancels (buffer cleared, selection reverts to None, focus back to dialog).
    Selecting one of the other three radios disables "One of" but preserves the buffer for later
    re-use.

    Keys: ``up`` / ``down`` move the cursor between rows; ``left`` / ``right`` move within a row
    (wrap); ``space`` toggles the option under the cursor (multi-toggle on row 0; radio-select on
    row 1); ``r`` clears both filter axes (and the One-of buffer); ``f`` / ``escape`` dismiss;
    ``s`` / ``e`` swap dialogs.

    Both rows push to the VM immediately on toggle — no separate "apply" key.
    """

    BINDINGS = [
        Binding("up", "cursor_up", show=False),
        Binding("down", "cursor_down", show=False),
        Binding("left", "cursor_left", show=False),
        Binding("right", "cursor_right", show=False),
        Binding("space", "toggle", show=False),
        Binding("r", "reset", show=False),
        Binding("s", "swap_to('sort')", show=False),
        # ``f`` toggles the dialog closed (symmetric with the ``f``-opens-it binding on the entries
        # table).
        Binding("f", "cancel", show=False),
        Binding("e", "swap_to('edit')", show=False),
        Binding("escape", "cancel", show=False),
    ]

    def __init__(
        self,
        view_model: KnowledgeEntryBrowserTabViewModel,
        tab: "KnowledgeEntryBrowserTabView",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model
        self._tab = tab
        # Two-axis cursor: ``_row`` picks the filter row (0 = type, 1 = flashcards), ``_col`` is the
        # column within that row. Rows have different option counts, so ``_col`` is clamped on row
        # change. The flashcards row's last column (index == len(_FLASHCARD_OPTIONS)) is the
        # view-side "One of" pseudo-option.
        self._row: int = 0
        self._col: int = 0
        # "One of" view-side state. ``_one_of_selected`` overrides the VM-derived bullet on the
        # flashcards row; ``_one_of_buffer`` is the current text. Both persist across the dialog's
        # open/close lifecycle (the dialog is mounted once and reused) — the buffer survives even
        # when the user switches the radio to another option so they can return to it later.
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
        # Input starts disabled (greyed out) since "One of" isn't selected by default.
        self.query_one("#one-of-input", _OneOfInput).disabled = True
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    def on_focus(self) -> None:
        self.call_after_refresh(self._refresh)

    def on_blur(self) -> None:
        self.call_after_refresh(self._refresh)

    def prepare_for_show(self) -> None:
        """Park the cursor at row 0, col 0 on each open. The active selections (VM filters + the
        view-side ``_one_of_*`` state) persist across opens by design."""
        self._row = 0
        self._col = 0

    def _row_lengths(self) -> tuple[int, int]:
        # Row 1 now has one extra column for the "One of" pseudo-option.
        return (len(_TYPE_OPTIONS), len(_FLASHCARD_OPTIONS) + 1)

    def _one_of_col(self) -> int:
        """Index of the "One of" pseudo-option on the flashcards row."""
        return len(_FLASHCARD_OPTIONS)

    def _refresh(self) -> None:
        try:
            type_row = self.query_one("#type-row", Static)
            radios = self.query_one("#flashcard-radios", Static)
            close_bracket = self.query_one("#one-of-close-bracket", Static)
            hint = self.query_one("#hint-row", Static)
            input_widget = self.query_one("#one-of-input", _OneOfInput)
        except Exception:
            # compose() hasn't finished yet — on_mount's call will catch up.
            return
        cursor_color = "bold #ffd700" if self.has_focus else "bold #6a6a6a"
        type_row.update(self._render_type_row(cursor_color))
        radios.update(self._render_flashcard_radios(cursor_color))
        close_bracket.update(self._render_close_bracket())
        hint.update(self._render_hint())
        # Sync the Input's enabled/value state with the view-side flags. Skip the value-mirror when
        # the input has focus so we don't clobber what the user is typing mid-keystroke.
        input_widget.disabled = not self._one_of_selected
        if not input_widget.has_focus and input_widget.value != self._one_of_buffer:
            input_widget.value = self._one_of_buffer

    def _selected_types(self) -> set[EntryType]:
        """Derive the current type selection from ``vm.entry_types``. ``None`` = all selected."""
        if self._vm.entry_types is None:
            return set(_TYPE_OPTIONS)
        return set(self._vm.entry_types)

    def _type_filter_active(self) -> bool:
        """True when the type filter is narrowing the view (not "all types selected")."""
        return self._selected_types() != set(_TYPE_OPTIONS)

    def _flashcard_filter_active(self) -> bool:
        """True when the flashcard-presence filter is narrowing the view. ``_one_of_selected``
        covers the case where the user has chosen "One of" but not yet submitted a buffer (so the
        VM axes are both still ``None``) — we still want the row header to read as active."""
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
        """Render the three real radio options + the "One of:" label. The compact Input itself is
        a sibling widget — this Static only renders text up through the "One of:" label and a
        trailing space; the Input lays out to the right inside the Horizontal."""
        text = Text()
        active = self._vm.has_flashcards
        lead_style = "bold #5fd75f" if self._flashcard_filter_active() else "dim"
        text.append(_pad_lead("filter by flashcards:"), style=lead_style)
        for i, (label, value) in enumerate(_FLASHCARD_OPTIONS):
            is_cursor = self._row == 1 and self._col == i
            # When "One of" is active, none of the real radios are selected (the bullet jumps to
            # the One-of pseudo-option) — we suppress the VM-derived bullet rather than rely on
            # ``vm.has_flashcards`` being None, since "None" is a legitimate VM value too.
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
        # "One of:" pseudo-option — bullet driven by ``_one_of_selected``. Trailing "[" demarcates
        # the start of the input area; the closing "]" lives in its own Static after the Input.
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
        # Hint line — extended with the selection-clearing warning while multi-select is on
        # (mirrors ``_SortBar``'s pattern).
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
        """Dispatch ``space`` based on the active row.

        Row 0 (types) — flip the cursor's option in the selection, then push the new filter to the
        VM. The VM's ``set_type_filter`` collapses "all selected" back to ``None``.

        Row 1 (flashcards):
          * One of the three real radios — radio-select: the cursor's option becomes the active
            value. Disables "One of" but preserves the buffer.
          * The "One of" pseudo-option — activates it (clearing any prior VM flashcard filter,
            since the two are mutually exclusive) and focuses the compact Input. Re-pressing
            ``space`` on it while already active just re-focuses the input — useful for resuming
            edits after a submit.
        """
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
                # Preserve enum-definition order so the kwargs snapshot is stable across toggles.
                self._vm.set_type_filter(tuple(t for t in _TYPE_OPTIONS if t in selected))
            return
        if self._col == self._one_of_col():
            self._activate_one_of()
        else:
            _, value = _FLASHCARD_OPTIONS[self._col]
            self._one_of_selected = False
            self._vm.set_flashcard_filter(value)

    def _activate_one_of(self) -> None:
        """Flip the radio to "One of" and focus the input. Idempotent on the selection flag — used
        both for the initial activation and for re-entry on subsequent ``space`` presses."""
        if not self._one_of_selected:
            self._one_of_selected = True
            # Clear any prior VM-side flashcard filter — the two are mutually exclusive at the
            # display layer, and leaving a stale VM value would render an incoherent state if the
            # user later clicks back to the VM-derived radios.
            self._vm.set_flashcard_filter(None)
        self._refresh()
        input_widget = self.query_one("#one-of-input", _OneOfInput)
        input_widget.disabled = False
        input_widget.focus()

    def submit_one_of(self, value: str) -> None:
        """Called from the input on ``enter``. Persist the buffer, parse it into a tuple of ids,
        and push the result to the VM. Focus returns to the dialog so the user can resume cursor
        navigation. The selection stays on "One of".

        Parsing is lenient: split on commas, strip whitespace, drop empty tokens, drop tokens that
        don't parse as ``int``. An empty result (either an empty buffer or all-garbage) clears
        the VM filter rather than applying the empty-set "no rows" sentinel — submitting nothing
        is a more natural "stop filtering" signal than escape, which also clears the buffer.
        """
        self._one_of_buffer = value
        ids = _parse_id_list(value)
        if ids:
            self._vm.set_flashcard_ids_filter(ids)
        else:
            # No usable ids → clear the VM-side filter, but stay in "One of" mode so the user can
            # keep editing. Mirrors how the search bar treats an empty query.
            self._vm.set_flashcard_ids_filter(None)
        self._refresh()
        self.focus()

    def cancel_one_of(self) -> None:
        """Called from the input on ``escape``. Per the spec: clears the buffer and reverts the
        chosen filter to None. Focus returns to the dialog."""
        self._one_of_buffer = ""
        self._one_of_selected = False
        # Drop both flashcard-filter axes — escape is the "back to no filter" exit.
        self._vm.set_flashcard_filter(None)
        self._refresh()
        self.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # _OneOfInput is the only Input mounted under this dialog; checking the type is enough to
        # distinguish from events bubbling from sibling subtrees (which won't reach here anyway,
        # since events bubble up — but the check guards against any future re-parenting).
        if isinstance(event.input, _OneOfInput):
            self.submit_one_of(event.value)

    def action_reset(self) -> None:
        """Clear both filter axes at once (and the One-of buffer). Lands the cursor back at row 0 /
        col 0 so subsequent keystrokes start from a predictable position.

        ``set_flashcard_filter(None)`` wipes both the ``has_flashcards`` and ``flashcard_ids``
        axes thanks to the mutual-exclusion invariant in the VM, so we don't need a separate
        call to clear the ids axis."""
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
    """Right-pad a filter-row lead-in to the shared column width so the option columns line up
    vertically across rows."""
    return label.ljust(_FILTER_LEAD_WIDTH)


def _parse_id_list(text: str) -> tuple[int, ...]:
    """Parse a comma-separated list of integer ids from the One-of buffer. Tokens that don't
    parse as ``int`` (or are empty after stripping) are dropped silently — keeps the UX lenient
    so a stray comma or partial edit doesn't reject the whole submission."""
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
