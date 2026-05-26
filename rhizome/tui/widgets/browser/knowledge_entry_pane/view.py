"""KnowledgeEntryBrowserPaneView — DataTable + details + status row + four pop-up dialogs (delete /
sort / filter / edit).

The pane view owns *interaction state*: which dialog is currently visible, where each dialog's
cursor lives, focus management on show/hide. The VM (see ``view_model.py``) owns *data facts* (the
loaded window, sort/search/filter values, selection) and the bulk-action API the dialogs eventually
invoke. The dialogs talk to the VM through that narrow surface (``set_sort``, ``apply_filter``,
``delete_selected_entries``, ``change_topic_on_selected_entries``,
``change_type_on_selected_entries``) and Textual's focus mechanics carry keystrokes the rest of the
way.
"""

from __future__ import annotations

from typing import Any, Literal

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Static, TextArea

from rhizome.db.models import EntryType
from rhizome.db.operations import EntrySortKey

from .entry_details import EntryDetailsView
from .linked_flashcards import LinkedFlashcardsPaneView
from .view_model import KnowledgeEntryBrowserPaneViewModel

# Sort axes the dialog surfaces. Ordered left-to-right the way they're laid out (matches the data
# table's column order). The DB op accepts a wider set; the dialog deliberately surfaces the four
# most useful axes.
_SORT_OPTIONS: tuple[EntrySortKey, ...] = ("id", "title", "type", "topic")

# Edit-dialog action choices, ordered left-to-right as shown to the user. ``edit title`` /
# ``edit content`` only appear in single-select mode (they refocus the corresponding details
# TextArea, which has no useful meaning for a bulk edit). Order matters: the destructive ``delete``
# sits last so the cursor never lands on it without an explicit rightward step.
_EDIT_OPTIONS_SINGLE: tuple[str, ...] = (
    "change topic",
    "change type",
    "edit title",
    "edit content",
    "delete",
)
_EDIT_OPTIONS_MULTI: tuple[str, ...] = (
    "change topic",
    "change type",
    "delete",
)

# Entry-type filter options. The view's only filter today; mirrors ``EntryType`` enum order.
_TYPE_OPTIONS: tuple[EntryType, ...] = tuple(EntryType)

_DialogName = Literal["delete", "sort", "filter", "edit"]


class _EntriesTable(DataTable):
    """``DataTable`` subclass that owns the multi-select keybindings and the dialog-toggle
    keybindings.

    Lives here rather than as standalone bindings on the parent view so the keys only fire when the
    table is focused — ``m`` and ``space`` on the details panel's ``TextArea``s would otherwise have
    to be suppressed. Selection actions delegate straight to the pane VM; dialog-toggle actions ask
    the parent pane to show/hide the named dialog.
    """

    BINDINGS = [
        Binding("m", "toggle_multi_select", show=False),
        Binding("space", "toggle_selection", show=False),
        # ``shift+up`` / ``shift+down`` are range-select sugar: add the cursor row to the selection
        # (idempotent) and step the cursor in one keystroke. Held-key terminal repeat makes "hold
        # shift, hold down" sweep a contiguous block. No-op outside multi-select (the VM guards).
        # Bound here rather than as ``"shift+up,shift+down"`` action pairs because each direction
        # needs its own cursor step.
        Binding("shift+down", "select_down", show=False),
        Binding("shift+up", "select_up", show=False),
        # ``d`` / ``s`` / ``f`` / ``e`` toggle the four dialogs. The pane owns which is currently
        # shown and runs the mutex (showing one hides the others).
        Binding("d", "toggle_dialog('delete')", show=False),
        Binding("s", "toggle_dialog('sort')", show=False),
        Binding("f", "toggle_dialog('filter')", show=False),
        Binding("e", "toggle_dialog('edit')", show=False),
        # ``ctrl+f`` flips the pane between ``ENTRIES`` (default) and ``LINKED_FLASHCARDS`` views.
        # First-pass binding — the user can refine the keystroke later. Lives on the table (not
        # the parent pane with priority) so it doesn't compete with ``ctrl+f`` in editing surfaces.
        Binding("ctrl+f", "toggle_state", show=False),
    ]

    def __init__(
        self,
        view_model: KnowledgeEntryBrowserPaneViewModel,
        pane: "KnowledgeEntryBrowserPaneView",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model
        self._pane = pane

    def action_toggle_multi_select(self) -> None:
        self._vm.toggle_multi_select()

    def action_toggle_selection(self) -> None:
        self._vm.toggle_current_selection()

    async def action_select_down(self) -> None:
        """Add the current row to the selection, then step the cursor down. Cursor step uses
        ``action_cursor_down`` (our overridden async version, which also handles the load-more-at-
        bottom case) so the usual ``RowHighlighted`` event fires and the VM cursor stays in sync."""
        self._vm.add_current_to_selection()
        await self.action_cursor_down()

    def action_select_up(self) -> None:
        self._vm.add_current_to_selection()
        self.action_cursor_up()

    async def action_cursor_down(self) -> None:
        """Cursor-down with auto-load at the bottom edge: if the user is on the last loaded row and
        the VM still has more to fetch, await ``load_more`` first so the next
        ``super().action_cursor_down`` has somewhere to land. ``load_more`` is a no-op when nothing
        further is available or a fetch is already in flight, so this is safe to call without
        re-checking those conditions.

        The cursor advance happens *after* the await — by then the VM has appended rows + emitted
        dirty, ``_refresh`` ran in ``extend`` mode, and the table has the new rows mounted.
        """
        if (
            self._vm.has_more
            and self.row_count > 0
            and self.cursor_row >= self.row_count - 1
        ):
            await self._vm.load_more()
        super().action_cursor_down()

    def action_toggle_dialog(self, name: str) -> None:
        self._pane.toggle_dialog(name)  # type: ignore[arg-type]

    def action_toggle_state(self) -> None:
        # Two-state toggle for now; if a third state lands, replace this with an explicit picker.
        current = self._vm.state
        target = (
            self._vm.State.LINKED_FLASHCARDS
            if current is self._vm.State.ENTRIES
            else self._vm.State.ENTRIES
        )
        self._vm.transition_to(target)


class _SearchInput(Input):
    """Search box mounted above the entries table.

    Visually mirrors the entry-detail title field: 3-row tight box, transparent background,
    ``#3a3a3a`` border that flips accent on focus. The keybinding hint rides the top border on the
    right (``border_title`` + ``border_title_align = "right"``) — same space the dialogs use for
    their hint lines, but here we save a full row by fusing it into the border.

    Input state:
      * ``enter`` — submit current buffer to ``vm.set_search``.
      * ``esc`` × 2 — clear buffer + submit empty query (reset). The first esc arms; the second
        clears. Any non-``esc`` key disarms, so a stray esc followed by editing doesn't leave the
        next esc as a surprise nuke.

    The state machine lives here (rather than on a parent wrapper) because ``Input`` consumes
    character keystrokes before they bubble, so a parent ``on_key`` would never see "user typed
    something" — the signal we need to disarm.
    """

    DEFAULT_CSS = """
    _SearchInput {
        background: transparent;
        border: solid #3a3a3a;
        height: 3;
        padding: 0 1;
    }
    _SearchInput:focus {
        border: solid $accent;
    }
    """

    BINDINGS = [
        Binding("escape", "handle_escape", show=False),
    ]

    def __init__(
        self,
        view_model: KnowledgeEntryBrowserPaneViewModel,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model
        self.armed_for_clear: bool = False
        # Border-title hint mounted to the right of the top border, mirroring how IDE search boxes
        # surface their keyboard hints at the edge of the box rather than in a separate row.
        self.border_title_align = "right"
        self._refresh_title()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Submit handler lives on the input itself rather than the pane view so the search bar is
        # self-contained — the only outward coupling is the ``vm.set_search`` call.
        if event.input is not self:
            return
        self._vm.set_search(event.value)

    def action_handle_escape(self) -> None:
        if self.armed_for_clear:
            self.value = ""
            self._vm.set_search("")
            self.armed_for_clear = False
        else:
            self.armed_for_clear = True
        self._refresh_title()

    def on_key(self, event) -> None:
        """Disarm on any non-escape key. Runs before the binding dispatch (so escape's own action
        still fires) and before ``Input``'s default character-insertion handling (so editing still
        works untouched)."""
        if event.key != "escape" and self.armed_for_clear:
            self.armed_for_clear = False
            self._refresh_title()

    def _refresh_title(self) -> None:
        if self.armed_for_clear:
            self.border_title = "[bold #ff8787]press esc again to clear[/]"
        else:
            self.border_title = "[dim]enter to submit • esc × 2 to clear[/]"


# ----------------------------------------------------------------------
# Dialog widgets
# ----------------------------------------------------------------------
#
# Each dialog widget owns its own cursor state and renders against a mix of local state + VM
# read-only attributes. Actions either invoke the VM's narrow action API (``set_sort``,
# ``apply_filter``, ``delete_selected_entries``) or ask the pane to swap to a sibling dialog
# (``pane.toggle_dialog``). Show/hide and focus rescue live on the pane; dialogs expose a
# ``prepare_for_show`` hook so they can initialize their cursor when the pane reveals them.


class _DeleteConfirm(Static, can_focus=True):
    """Delete confirmation dialog. Targets the multi-select selection or the cursor entry depending
    on mode (the VM's ``delete_selected_entries`` resolves this internally).

    Renders three lines: a header explaining the action (entry count + the no-flashcards-harmed
    promise), then two indented choice rows (Confirm / Cancel). Cursor brightness tracks focus.
    """

    BINDINGS = [
        Binding("up", "choice_up", show=False),
        Binding("down", "choice_down", show=False),
        Binding("enter", "choice_confirm", show=False),
        Binding("escape", "cancel", show=False),
        # Mutex siblings: pressing one of these from inside the delete dialog swaps to the other.
        Binding("s", "swap_to('sort')", show=False),
        Binding("f", "swap_to('filter')", show=False),
        Binding("e", "swap_to('edit')", show=False),
    ]

    def __init__(
        self,
        view_model: KnowledgeEntryBrowserPaneViewModel,
        pane: "KnowledgeEntryBrowserPaneView",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model
        self._pane = pane
        # 0 = Confirm, 1 = Cancel. Reset to 0 every time the dialog is shown (``prepare_for_show``).
        self._choice_cursor: int = 0

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    def on_focus(self) -> None:
        # Cursor brightness tracks focus.
        self.call_after_refresh(self._refresh)

    def on_blur(self) -> None:
        self.call_after_refresh(self._refresh)

    def prepare_for_show(self) -> None:
        """Called by the pane right before this dialog becomes visible. Reset the choice cursor to
        ``Confirm`` so each open starts fresh."""
        self._choice_cursor = 0

    def _refresh(self) -> None:
        # Note: not ``_render`` — that's a Textual-internal name (the widget's own ``_render``
        # returns the cached Visual). Naming this method ``_render`` shadows the framework hook and
        # Textual tries to use the returned ``rich.text.Text`` as a ``Visual``, blowing up in
        # ``to_strips``.
        self.update(self._render_dialog())

    def _render_dialog(self) -> Text:
        count = self._pane.selection_target_count()
        noun = "entry" if count == 1 else "entries"
        # In single-select mode the lead-in is just "Delete this entry?" — "selected" reads weird
        # when there's no visible selection mark. Multi-select keeps the existing phrasing.
        scope_word = "selected " if self._vm.multi_select_active else ""
        cursor_style = "bold" if self.has_focus else "#6a6a6a"
        text = Text()
        text.append(f"Delete {count} {scope_word}{noun}? ", style="bold")
        text.append("Linked flashcards will not be affected.", style="dim")
        text.append("\n")
        labels = ("Confirm", "Cancel")
        for i, label in enumerate(labels):
            chosen = i == self._choice_cursor
            if chosen:
                text.append("► ", style=cursor_style)
                text.append(label, style="bold")
            else:
                text.append("  ")
                text.append(label, style="dim")
            if i < len(labels) - 1:
                text.append("\n")
        return text

    def action_choice_up(self) -> None:
        self._choice_cursor = (self._choice_cursor - 1) % 2
        self._refresh()

    def action_choice_down(self) -> None:
        self._choice_cursor = (self._choice_cursor + 1) % 2
        self._refresh()

    async def action_choice_confirm(self) -> None:
        if self._choice_cursor == 0:
            await self._vm.delete_selected_entries()
        self._pane.hide_dialog()

    def action_cancel(self) -> None:
        self._pane.hide_dialog()

    def action_swap_to(self, name: str) -> None:
        self._pane.toggle_dialog(name)  # type: ignore[arg-type]


class _SortBar(Static, can_focus=True):
    """Sort-axis picker dialog. Sits in the same screen slot as the other dialogs (the pane runs the
    mutex).

    Renders horizontally, mirroring the data table's column order: ``id   title   type   topic``.
    The active sort is decorated with an arrow (``↑`` / ``↓``) and brackets; the cursor option is
    shown in a bold accent colour (no ``►`` prefix — keeping the row at a fixed width avoids labels
    jumping around as the cursor moves). A second line carries a help hint, extended with a
    selection-clearing warning while multi-select is on.

    Keys: ``left`` / ``right`` move the cursor (with wrap); ``enter`` applies (toggles direction
    when on the active axis, otherwise switches to that axis ascending); ``s`` and ``escape``
    dismiss without applying.
    """

    BINDINGS = [
        Binding("left", "cursor_left", show=False),
        Binding("right", "cursor_right", show=False),
        Binding("enter", "apply", show=False),
        # ``r`` resets to the default sort (``id`` ascending). Mirrors the same key in the filter
        # dialog.
        Binding("r", "reset", show=False),
        # ``s`` toggles the dialog closed — symmetric with the ``s``-opens-it binding on the
        # entries table.
        Binding("s", "cancel", show=False),
        Binding("f", "swap_to('filter')", show=False),
        Binding("e", "swap_to('edit')", show=False),
        Binding("escape", "cancel", show=False),
    ]

    def __init__(
        self,
        view_model: KnowledgeEntryBrowserPaneViewModel,
        pane: "KnowledgeEntryBrowserPaneView",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model
        self._pane = pane
        # Cursor index into ``_SORT_OPTIONS``. Landed on the currently-active sort axis at
        # ``prepare_for_show``; moves with ←/→.
        self._cursor: int = 0

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    def on_focus(self) -> None:
        self.call_after_refresh(self._refresh)

    def on_blur(self) -> None:
        self.call_after_refresh(self._refresh)

    def prepare_for_show(self) -> None:
        """Land the cursor on the currently-active sort axis so the most common action (toggle the
        direction of the active sort) is one ``enter`` away. Falls back to ``id`` (index 0) if the
        active axis isn't surfaced in the dialog (e.g. legacy ``created_at`` from an older
        session)."""
        try:
            self._cursor = _SORT_OPTIONS.index(self._vm.sort_by)
        except ValueError:
            self._cursor = 0

    def _refresh(self) -> None:
        self.update(self._render_bar())

    def _render_bar(self) -> Text:
        active_idx = (
            _SORT_OPTIONS.index(self._vm.sort_by)
            if self._vm.sort_by in _SORT_OPTIONS
            else -1
        )
        arrow = "↑" if self._vm.sort_dir == "asc" else "↓"
        # Cursor colour: bright gold on focus, dim grey otherwise. The active axis itself always
        # renders in the default fg so the arrow + brackets carry the "this is the live sort"
        # signal.
        cursor_color = "bold #ffd700" if self.has_focus else "bold #6a6a6a"

        text = Text()
        for i, option in enumerate(_SORT_OPTIONS):
            is_active = i == active_idx
            is_cursor = i == self._cursor
            label = f"{arrow}[{option}]" if is_active else option
            if is_cursor:
                style = cursor_color
            elif is_active:
                style = ""  # default fg
            else:
                style = "#787878"
            text.append(label, style=style)
            if i < len(_SORT_OPTIONS) - 1:
                text.append("   ")
        text.append("\n")

        # Help line — extended with the selection-clearing warning when in multi-select. The
        # warning sits inline rather than on a third line so the dialog stays at a fixed 4-line
        # height across both modes.
        hint = Text()
        hint.append(
            "← / → move • enter apply • r reset • s/esc dismiss", style="dim",
        )
        if self._vm.multi_select_active:
            hint.append("   ", style="dim")
            hint.append("Applying clears your selection.", style="#ff8787")
        text.append(hint)
        return text

    def action_cursor_left(self) -> None:
        self._cursor = (self._cursor - 1) % len(_SORT_OPTIONS)
        self._refresh()

    def action_cursor_right(self) -> None:
        self._cursor = (self._cursor + 1) % len(_SORT_OPTIONS)
        self._refresh()

    def action_apply(self) -> None:
        """Header-click semantic: same axis → toggle direction; different axis → switch to that axis
        ascending. Dialog stays open so the user can keep tweaking."""
        chosen = _SORT_OPTIONS[self._cursor]
        if chosen == self._vm.sort_by:
            new_dir: Literal["asc", "desc"] = (
                "desc" if self._vm.sort_dir == "asc" else "asc"
            )
        else:
            new_dir = "asc"
        self._vm.set_sort(chosen, new_dir)

    def action_reset(self) -> None:
        """Restore the default sort (``id`` ascending). Lands the cursor on ``id`` regardless of
        whether the sort actually changes — the dialog stays open."""
        self._cursor = 0
        self._vm.set_sort("id", "asc")

    def action_cancel(self) -> None:
        self._pane.hide_dialog()

    def action_swap_to(self, name: str) -> None:
        self._pane.toggle_dialog(name)  # type: ignore[arg-type]


class _FilterDialog(Static, can_focus=True):
    """Entry-type filter picker. Sits in the same screen slot as the other dialogs.

    The selection state derives directly from ``vm.entry_types``: ``None`` means "all types selected"
    (no filter); a tuple restricts to those types. Toggling an option recomputes the new set and
    calls ``vm.apply_filter`` immediately — there's no separate "apply" key.

    Keys: ``left`` / ``right`` move the cursor (with wrap); ``space`` toggles the option under the
    cursor; ``r`` resets to no-filter; ``f`` / ``escape`` dismiss; ``s`` / ``e`` swap dialogs.
    """

    BINDINGS = [
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
        view_model: KnowledgeEntryBrowserPaneViewModel,
        pane: "KnowledgeEntryBrowserPaneView",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model
        self._pane = pane
        # Cursor index into ``_TYPE_OPTIONS``.
        self._cursor: int = 0

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    def on_focus(self) -> None:
        self.call_after_refresh(self._refresh)

    def on_blur(self) -> None:
        self.call_after_refresh(self._refresh)

    def prepare_for_show(self) -> None:
        """Park the cursor at index 0 on each open. The active selection is read directly from the
        VM, so nothing else to sync here."""
        self._cursor = 0

    def _refresh(self) -> None:
        self.update(self._render_dialog())

    def _selected_types(self) -> set[EntryType]:
        """Derive the current selection from ``vm.entry_types``. ``None`` = all selected."""
        if self._vm.entry_types is None:
            return set(_TYPE_OPTIONS)
        return set(self._vm.entry_types)

    def _is_default(self) -> bool:
        """True when no filter is active (every type selected). Used for the "filter is narrowing"
        green-tint highlight in the header."""
        return self._selected_types() == set(_TYPE_OPTIONS)

    def _render_dialog(self) -> Text:
        cursor_color = "bold #ffd700" if self.has_focus else "bold #6a6a6a"
        selected = self._selected_types()
        narrow_color = "#5fd75f" if not self._is_default() else ""

        text = Text()
        # Line 1 — lead-in + option row. Lead-in gets a green tint when the filter is active so the
        # user can spot at a glance that this dialog is narrowing the view.
        lead_style = ("bold " + narrow_color).strip() if narrow_color else "dim"
        text.append("filter by type:  ", style=lead_style)
        for i, opt in enumerate(_TYPE_OPTIONS):
            is_cursor = i == self._cursor
            is_sel = opt in selected
            marker = "[x]" if is_sel else "[ ]"
            marker_style = "#5fd75f" if is_sel else "#787878"
            label_style = cursor_color if is_cursor else ""
            text.append(marker, style=marker_style)
            text.append(" ")
            text.append(opt.value, style=label_style)
            if i < len(_TYPE_OPTIONS) - 1:
                text.append("    ")
        text.append("\n")

        # Line 2 — hint, extended with the selection-clearing warning while multi-select is on
        # (mirrors ``_SortBar``'s pattern).
        hint = Text()
        hint.append(
            "← / → move • space toggle • r reset • f/esc dismiss", style="dim",
        )
        if self._vm.multi_select_active:
            hint.append("   ", style="dim")
            hint.append("Toggling clears your selection.", style="#ff8787")
        text.append(hint)
        return text

    def action_cursor_left(self) -> None:
        self._cursor = (self._cursor - 1) % len(_TYPE_OPTIONS)
        self._refresh()

    def action_cursor_right(self) -> None:
        self._cursor = (self._cursor + 1) % len(_TYPE_OPTIONS)
        self._refresh()

    def action_toggle(self) -> None:
        """Flip the cursor's option in the selection, then push the new filter to the VM. The VM's
        ``apply_filter`` collapses "all selected" back to ``None``."""
        target = _TYPE_OPTIONS[self._cursor]
        selected = self._selected_types()
        if target in selected:
            selected.discard(target)
        else:
            selected.add(target)
        if selected == set(_TYPE_OPTIONS):
            self._vm.apply_filter(None)
        else:
            # Preserve enum-definition order so the kwargs snapshot is stable across toggles.
            self._vm.apply_filter(tuple(t for t in _TYPE_OPTIONS if t in selected))

    def action_reset(self) -> None:
        self._vm.apply_filter(None)

    def action_cancel(self) -> None:
        self._pane.hide_dialog()

    def action_swap_to(self, name: str) -> None:
        self._pane.toggle_dialog(name)  # type: ignore[arg-type]


class _TypePickerScreen(ModalScreen[EntryType | None]):
    """Modal screen for picking an ``EntryType``. Three options laid out vertically; arrows / enter
    / escape. Dismisses with the chosen ``EntryType`` (caller applies it) or ``None`` on cancel.

    Deliberately co-located with the pane view rather than under ``tui/screens/`` — the picker is
    tiny and only used here, so the extra indirection isn't worth it. If a second consumer shows
    up, lift it out.
    """

    DEFAULT_CSS = """
    _TypePickerScreen {
        align: center middle;
    }
    _TypePickerScreen > Vertical {
        width: 40;
        height: auto;
        border: solid $surface-lighten-2;
        padding: 1 2;
        background: $surface;
    }
    _TypePickerScreen Static {
        color: rgb(150,150,150);
    }
    _TypePickerScreen #type-picker-header {
        margin-bottom: 1;
        color: rgb(100,100,100);
    }
    """

    BINDINGS = [
        Binding("up", "cursor_up", show=False),
        Binding("down", "cursor_down", show=False),
        Binding("enter", "select", show=False),
        Binding("escape", "cancel", show=False),
    ]

    def __init__(self, *, current: EntryType | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._options: tuple[EntryType, ...] = tuple(EntryType)
        # Land the cursor on the current type when there is one, so the most common "I want to
        # change to something other than this" flow is one ``down`` away.
        if current is not None and current in self._options:
            self._cursor = self._options.index(current)
        else:
            self._cursor = 0

    def compose(self):
        with Vertical():
            yield Static(
                "Select entry type  (↑/↓ navigate, enter select, esc cancel)",
                id="type-picker-header",
            )
            yield Static(self._render_options(), id="type-picker-options")

    def _render_options(self) -> Text:
        text = Text()
        for i, opt in enumerate(self._options):
            is_cursor = i == self._cursor
            if is_cursor:
                text.append("► ", style="bold #ffd700")
                text.append(opt.value, style="bold")
            else:
                text.append("  ")
                text.append(opt.value, style="dim")
            if i < len(self._options) - 1:
                text.append("\n")
        return text

    def _repaint(self) -> None:
        self.query_one("#type-picker-options", Static).update(self._render_options())

    def action_cursor_up(self) -> None:
        self._cursor = (self._cursor - 1) % len(self._options)
        self._repaint()

    def action_cursor_down(self) -> None:
        self._cursor = (self._cursor + 1) % len(self._options)
        self._repaint()

    def action_select(self) -> None:
        self.dismiss(self._options[self._cursor])

    def action_cancel(self) -> None:
        self.dismiss(None)


class _EditBar(Static, can_focus=True):
    """Edit-action picker. Sits in the same screen slot as the other dialogs.

    Renders horizontally: option list on one line, hint on the next. Options come from a local
    constant pair (``_EDIT_OPTIONS_SINGLE`` / ``_EDIT_OPTIONS_MULTI``); multi-select hides the
    per-entry edit shortcuts since they have no useful meaning for a bulk edit.

    Keys: ``left`` / ``right`` move the cursor (wrap); ``enter`` dispatches the highlighted choice;
    ``e`` / ``escape`` dismiss; ``s`` / ``f`` / ``d`` swap to the corresponding sibling dialog.

    Dispatch sits on the pane (``handle_edit_choice``) because two of the choices — ``change topic``
    and ``change type`` — open modal screens, and the other two (``edit title`` / ``edit content``)
    are pure focus shortcuts to the details panel. The bar's job is to forward the highlighted
    choice string.
    """

    BINDINGS = [
        Binding("left", "cursor_left", show=False),
        Binding("right", "cursor_right", show=False),
        Binding("enter", "select", show=False),
        # ``e`` toggles the dialog closed.
        Binding("e", "cancel", show=False),
        Binding("escape", "cancel", show=False),
        Binding("s", "swap_to('sort')", show=False),
        Binding("f", "swap_to('filter')", show=False),
        Binding("d", "swap_to('delete')", show=False),
    ]

    def __init__(
        self,
        view_model: KnowledgeEntryBrowserPaneViewModel,
        pane: "KnowledgeEntryBrowserPaneView",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model
        self._pane = pane
        # Cursor index into the active options list (mode-dependent).
        self._cursor: int = 0

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    def on_focus(self) -> None:
        self.call_after_refresh(self._refresh)

    def on_blur(self) -> None:
        self.call_after_refresh(self._refresh)

    def prepare_for_show(self) -> None:
        self._cursor = 0

    def _options(self) -> tuple[str, ...]:
        return (
            _EDIT_OPTIONS_MULTI
            if self._vm.multi_select_active
            else _EDIT_OPTIONS_SINGLE
        )

    def _refresh(self) -> None:
        # Clamp cursor in case the options list shrank under us (e.g. multi-select toggled on while
        # the dialog was open and the cursor was on a single-only option).
        opts = self._options()
        if opts and self._cursor >= len(opts):
            self._cursor = len(opts) - 1
        self.update(self._render_bar())

    def _render_bar(self) -> Text:
        cursor_color = "bold #ffd700" if self.has_focus else "bold #6a6a6a"
        text = Text()
        # Lead-in: "edit N entries:" / "edit this entry:" — gives the user a clear scope reminder
        # while they navigate.
        count = self._pane.selection_target_count()
        if self._vm.multi_select_active:
            noun = "entry" if count == 1 else "entries"
            text.append(f"edit {count} {noun}:  ", style="dim")
        else:
            text.append("edit this entry:  ", style="dim")
        options = self._options()
        for i, opt in enumerate(options):
            is_cursor = i == self._cursor
            style = cursor_color if is_cursor else "#787878"
            text.append(opt, style=style)
            if i < len(options) - 1:
                text.append("   ")
        text.append("\n")
        text.append("← / → move • enter select • e/esc dismiss", style="dim")
        return text

    def action_cursor_left(self) -> None:
        opts = self._options()
        if opts:
            self._cursor = (self._cursor - 1) % len(opts)
            self._refresh()

    def action_cursor_right(self) -> None:
        opts = self._options()
        if opts:
            self._cursor = (self._cursor + 1) % len(opts)
            self._refresh()

    async def action_select(self) -> None:
        """Forward the highlighted choice string to the pane for dispatch."""
        opts = self._options()
        if not opts or self._cursor < 0 or self._cursor >= len(opts):
            return
        await self._pane.handle_edit_choice(opts[self._cursor])

    def action_cancel(self) -> None:
        self._pane.hide_dialog()

    def action_swap_to(self, name: str) -> None:
        self._pane.toggle_dialog(name)  # type: ignore[arg-type]


class _EntryContentPreview(TextArea):
    """Read-only scrollable preview of the cursor entry's ``content`` field. Non-navigable
    (``can_focus=False``) so the keyboard never lands here; mouse-wheel scroll still works.

    Subscribes to the pane VM's ``dirty`` (refetches, post-save repaints) and the details VM's
    ``dirty`` (cursor moves — ``set_cursor`` routes through ``details.set_entry`` which fires the
    details dirty, but does **not** fire the pane dirty itself). Re-reads ``entries[cursor]`` on
    each fire and rebuilds the text.

    Only rendered when the parent pane is in ``State.LINKED_FLASHCARDS`` (CSS-driven via the
    ``-state-*`` class on the pane); in ``ENTRIES`` the details panel covers the same job, so
    showing both would be redundant.
    """

    can_focus = False

    DEFAULT_CSS = """
    _EntryContentPreview {
        background: transparent;
        border: solid #3a3a3a;
        padding: 0 1;
    }
    """

    def __init__(
        self,
        view_model: KnowledgeEntryBrowserPaneViewModel,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            read_only=True, show_line_numbers=False, soft_wrap=True, **kwargs,
        )
        self._vm = view_model
        self.border_title = "[dim]entry content[/]"
        self.border_title_align = "left"

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._vm.details.subscribe(self._vm.details.dirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)
        self._vm.details.unsubscribe(self._vm.details.dirty, self._refresh)

    def _refresh(self) -> None:
        entries = self._vm.entries
        cursor = self._vm.cursor
        if not entries or cursor >= len(entries):
            target = ""
        else:
            target = entries[cursor].content or ""
        if self.text != target:
            self.text = target


class KnowledgeEntryBrowserPaneView(Vertical):
    """Pane view for ``KnowledgeEntryBrowserPaneViewModel``: search bar + DataTable + status row,
    a details panel on the right (in ``ENTRIES``) or a linked-flashcards table (in
    ``LINKED_FLASHCARDS``), and four pop-up dialogs (delete / sort / filter / edit) along the
    bottom.

    Owns the dialog mutex via ``_active_dialog`` and the ``toggle_dialog`` / ``show_dialog`` /
    ``hide_dialog`` methods. The dialog widgets and the entries-table key bindings ask the pane to
    swap dialogs; the pane handles visibility (``-visible`` class) and focus rescue.
    """

    DEFAULT_CSS = """
    KnowledgeEntryBrowserPaneView {
        height: 1fr;
        layout: vertical;
        padding: 0 1;
    }
    KnowledgeEntryBrowserPaneView #pane-body {
        layout: horizontal;
        height: 1fr;
    }
    /* Left column of the pane body: search bar over the entries
       table. Width is set per-state below (60% in ENTRIES, 50% in
       LINKED_FLASHCARDS); the table fills its parent column. */
    KnowledgeEntryBrowserPaneView #table-column {
        height: 1fr;
        layout: vertical;
    }
    KnowledgeEntryBrowserPaneView #entries-table {
        width: 1fr;
        height: 1fr;
        margin: 1 0 0 0;
    }
    /* Entry-content preview sits below the entries table in the left column. Hidden by default
       (the ``ENTRIES`` state has the editable details panel on the right doing the same job); the
       ``-state-linked-flashcards`` rule below flips it on and rebalances the column to 2fr/1fr
       table/preview. */
    KnowledgeEntryBrowserPaneView #entry-content-preview {
        display: none;
    }
    KnowledgeEntryBrowserPaneView.-state-linked-flashcards #entries-table {
        height: 2fr;
    }
    KnowledgeEntryBrowserPaneView.-state-linked-flashcards #entry-content-preview {
        display: block;
        width: 1fr;
        height: 1fr;
        margin: 1 0 0 0;
    }
    /* Multi-select wash: keep the zebra alternation but shift both rows
       darker, so the table reads as muted-but-structured and the bright-
       green selected rows pop. */
    KnowledgeEntryBrowserPaneView #entries-table.-multi-select {
        background: $surface-darken-2;
    }
    KnowledgeEntryBrowserPaneView #entries-table.-multi-select > .datatable--even-row {
        background: $surface-darken-1 50%;
    }
    /* State-driven layout swap. Both right-hand views are mounted up
       front; the ``-state-*`` class toggles which is visible and the
       corresponding widths. */
    KnowledgeEntryBrowserPaneView.-state-entries #table-column {
        width: 60%;
    }
    KnowledgeEntryBrowserPaneView.-state-entries EntryDetailsView {
        width: 40%;
        height: 1fr;
        display: block;
    }
    KnowledgeEntryBrowserPaneView.-state-entries LinkedFlashcardsPaneView {
        display: none;
    }
    KnowledgeEntryBrowserPaneView.-state-linked-flashcards #table-column {
        width: 50%;
    }
    KnowledgeEntryBrowserPaneView.-state-linked-flashcards LinkedFlashcardsPaneView {
        width: 50%;
        height: 1fr;
        display: block;
    }
    KnowledgeEntryBrowserPaneView.-state-linked-flashcards EntryDetailsView {
        display: none;
    }
    /* Status row at the bottom of ``#table-column`` so it aligns with the linked-flashcards docked
       status row inside its own column. */
    KnowledgeEntryBrowserPaneView #pane-status {
        height: 1;
        color: $foreground-muted;
        text-style: dim;
        padding: 0 1;
    }
    KnowledgeEntryBrowserPaneView #delete-confirm {
        height: 4;
        margin: 1 0 0 0;
        padding: 0 1;
        border-top: solid #3a3a3a;
        color: rgb(200,200,200);
        display: none;
    }
    KnowledgeEntryBrowserPaneView #delete-confirm.-visible {
        display: block;
    }
    KnowledgeEntryBrowserPaneView #delete-confirm:focus {
        border-top: solid $accent;
    }
    KnowledgeEntryBrowserPaneView #sort-bar {
        height: 3;
        margin: 1 0 0 0;
        padding: 0 1;
        border-top: solid #3a3a3a;
        color: rgb(200,200,200);
        display: none;
    }
    KnowledgeEntryBrowserPaneView #sort-bar.-visible {
        display: block;
    }
    KnowledgeEntryBrowserPaneView #sort-bar:focus {
        border-top: solid $accent;
    }
    KnowledgeEntryBrowserPaneView #filter-dialog {
        height: 3;
        margin: 1 0 0 0;
        padding: 0 1;
        border-top: solid #3a3a3a;
        color: rgb(200,200,200);
        display: none;
    }
    KnowledgeEntryBrowserPaneView #filter-dialog.-visible {
        display: block;
    }
    KnowledgeEntryBrowserPaneView #filter-dialog:focus {
        border-top: solid $accent;
    }
    KnowledgeEntryBrowserPaneView #edit-bar {
        height: 3;
        margin: 1 0 0 0;
        padding: 0 1;
        border-top: solid #3a3a3a;
        color: rgb(200,200,200);
        display: none;
    }
    KnowledgeEntryBrowserPaneView #edit-bar.-visible {
        display: block;
    }
    KnowledgeEntryBrowserPaneView #edit-bar:focus {
        border-top: solid $accent;
    }
    """

    # Max display width for the title column. Anything longer is truncated by ``DataTable`` (with an
    # ellipsis).
    _TITLE_COLUMN_WIDTH = 50

    # Maps dialog name → (widget id, widget class). Used by ``toggle_dialog`` /
    # ``_refresh_dialog_visibility`` to dispatch generically.
    _DIALOG_WIDGETS: tuple[tuple[_DialogName, str], ...] = (
        ("delete", "#delete-confirm"),
        ("sort", "#sort-bar"),
        ("filter", "#filter-dialog"),
        ("edit", "#edit-bar"),
    )

    def __init__(
        self,
        view_model: KnowledgeEntryBrowserPaneViewModel,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model
        # Which dialog (if any) is currently visible. The mutex: at most one is shown at a time.
        # Mutators run through ``toggle_dialog`` / ``hide_dialog`` so visibility, focus, and
        # ``prepare_for_show`` stay coordinated.
        self._active_dialog: _DialogName | None = None
        # Tracks the VM's state at the last refresh so a state transition can auto-dismiss any open
        # dialog (dialogs that depend on the current state — selection actions, sort axes — aren't
        # generally meaningful across a layout switch).
        self._last_state: KnowledgeEntryBrowserPaneViewModel.State | None = None
        # Signature of the entries list at the last refresh — a tuple of entry ids in display order.
        # Used by ``_refresh`` to decide between a full ``clear()`` + rebuild (when row identity has
        # actually changed: refetch, delete, load_more) and a cheap in-place ``update_cell_at`` pass
        # (when only styles or markers changed: mode toggle, selection toggle, post-edit content
        # mutation). The in-place path preserves ``DataTable``'s scroll position and cursor.
        self._last_row_signature: tuple[int, ...] | None = None

    def compose(self):
        table = _EntriesTable(
            self._vm, self,
            id="entries-table", cursor_type="row", zebra_stripes=True,
        )
        # The leading "sel" column is always present (we can't add or drop columns cleanly after
        # construction). When multi-select is off the column renders empty; when on, each row shows
        # ``[ ]`` or ``[x]``.
        table.add_column("sel", width=3)
        table.add_column("id")
        table.add_column("title", width=self._TITLE_COLUMN_WIDTH)
        table.add_column("type")
        table.add_column("topic")
        table.add_column("flashcards")
        with Horizontal(id="pane-body"):
            with Vertical(id="table-column"):
                yield _SearchInput(self._vm, id="search-input")
                yield table
                # Preview only renders in ``LINKED_FLASHCARDS`` — CSS toggles ``display`` based on
                # the parent's ``-state-*`` class.
                yield _EntryContentPreview(self._vm, id="entry-content-preview")
                yield Static("", id="pane-status")

            # Both right-hand views are mounted up front and shown / hidden via the ``-state-*``
            # class on the parent. Mounting them once avoids the cost of re-subscribing each child
            # view's ``vm.dirty`` callback on every state flip.
            yield EntryDetailsView(self._vm.details)
            yield LinkedFlashcardsPaneView(self._vm.linked_flashcards)
        yield _DeleteConfirm(self._vm, self, id="delete-confirm")
        yield _SortBar(self._vm, self, id="sort-bar")
        yield _FilterDialog(self._vm, self, id="filter-dialog")
        yield _EditBar(self._vm, self, id="edit-bar")

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        # If the VM already has data (it was bootstrapped before the view mounted), paint it on
        # first frame instead of waiting for the next dirty.
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    # ------------------------------------------------------------------
    # Dialog orchestration
    # ------------------------------------------------------------------

    def toggle_dialog(self, name: _DialogName) -> None:
        """Toggle the named dialog. If it's already shown, hide it; otherwise show it (hiding any
        other). Called from the entries table's key bindings (d/e/f/s) and from sibling-dialog swap
        actions."""
        if self._active_dialog == name:
            self.hide_dialog()
        else:
            self.show_dialog(name)

    def show_dialog(self, name: _DialogName) -> None:
        """Show the named dialog and hide any other. No-op if it's already showing."""
        if self._active_dialog == name:
            return
        self._active_dialog = name
        self._refresh_dialog_visibility()

    def hide_dialog(self) -> None:
        """Hide whichever dialog is currently shown (if any). Refocuses the entries table."""
        if self._active_dialog is None:
            return
        self._active_dialog = None
        self._refresh_dialog_visibility()

    def _refresh_dialog_visibility(self) -> None:
        """Apply the ``-visible`` class to the active dialog (and remove it from the others), then
        run focus rescue: focus the newly-active dialog, or fall back to the entries table when
        nothing's active. Called whenever ``_active_dialog`` flips."""
        for name, widget_id in self._DIALOG_WIDGETS:
            try:
                widget = self.query_one(widget_id, Static)
            except Exception:
                # Compose hasn't finished yet — skip; mount-time _refresh will catch up.
                continue
            is_visible = name == self._active_dialog
            widget.set_class(is_visible, "-visible")
            if is_visible:
                prepare = getattr(widget, "prepare_for_show", None)
                if prepare is not None:
                    prepare()
                widget.focus()
        if self._active_dialog is None:
            try:
                self.query_one("#entries-table", DataTable).focus()
            except Exception:
                # Table may have been unmounted (e.g. pane swap mid-close); let focus settle wherever
                # Textual puts it.
                pass

    # ------------------------------------------------------------------
    # Selection-target helper (used by dialog rendering and dispatchers)
    # ------------------------------------------------------------------

    def selection_target_count(self) -> int:
        """Count of entries the current "selected" action would act on. In multi-select mode that's
        ``selected_ids``; in single-select mode it's the cursor entry (or zero if the window is
        empty)."""
        if self._vm.multi_select_active:
            return len(self._vm.selected_ids)
        return 1 if self._vm.entries else 0

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        table = self.query_one("#entries-table", DataTable)
        mode = self._vm.multi_select_active
        # ``-multi-select`` triggers the CSS that darkens the zebra-row palette while the user is
        # picking.
        table.set_class(mode, "-multi-select")

        # Apply the state class. The CSS rules keyed off ``.-state-entries`` /
        # ``.-state-linked-flashcards`` swap which right-hand view is visible and the corresponding
        # left-column width (60/40 vs 50/50).
        state = self._vm.state
        self.set_class(state is self._vm.State.ENTRIES, "-state-entries")
        self.set_class(state is self._vm.State.LINKED_FLASHCARDS, "-state-linked-flashcards")

        # State transition closes whichever dialog is open — its targets / sort axes / filter
        # options aren't generally meaningful across a layout switch, and the user is doing a bigger
        # navigation gesture than a dialog dismissal.
        if self._last_state is not None and state != self._last_state:
            if self._active_dialog is not None:
                self.hide_dialog()
        self._last_state = state

        # Three refresh paths, picked by comparing the new id-tuple to the previously-rendered one:
        #
        #   * ``extend`` — old tuple is a prefix of the new one, length grew. ``load_more`` appends
        #     rows; we ``add_row`` only the new tail. No ``clear``, no cursor restore, scroll stays
        #     where the user left it.
        #   * ``inplace`` — same tuple. Pure style/marker churn (multi-select toggle, selection
        #     toggle, post-edit content mutation). ``update_cell_at`` per cell preserves scroll +
        #     cursor.
        #   * ``rebuild`` — anything else (refetch, delete, reorder). ``clear`` + ``add_row``,
        #     restore cursor.
        new_signature = tuple(e.id for e in self._vm.entries)
        old_signature = self._last_row_signature
        if new_signature == old_signature:
            path = "inplace"
            start = 0
        elif (
            old_signature is not None
            and len(new_signature) > len(old_signature)
            and new_signature[: len(old_signature)] == old_signature
        ):
            path = "extend"
            start = len(old_signature)
        else:
            path = "rebuild"
            start = 0
            table.clear()

        for i in range(start, len(self._vm.entries)):
            entry = self._vm.entries[i]
            type_str = entry.entry_type.value if entry.entry_type is not None else "—"
            # Three colouring regimes:
            #   * not multi-select: zebra-pair text (odd rows dim) so the stripe background shows
            #     through evenly.
            #   * multi-select, not selected: same zebra-pair pattern but with both colours shifted
            #     darker — so the whole table reads as muted-but-structured.
            #   * multi-select, selected: bright green + bold to pop against the dimmed sea around
            #     them.
            selected = mode and entry.id in self._vm.selected_ids
            if selected:
                style = "bold #5fd75f"
            elif mode:
                style = "#787878" if i % 2 else "#a0a0a0"
            else:
                style = "#a0a0a0" if i % 2 else ""
            marker = ("[x]" if selected else "[ ]") if mode else ""
            # Topic column shows the topic name followed by " [{id}]" in a fixed dim grey — matches
            # the topic tree's hint style. The defensive fallback to ``topic_id`` is here in case
            # something ever lands an entry whose topic FK isn't loaded.
            topic_name = entry.topic.name if entry.topic is not None else "?"
            topic_cell = Text.assemble(
                (topic_name, style),
                (f" [{entry.topic_id}]", "#787878"),
            )
            # Flashcard ids are sorted for stable display order — the ``flashcard_entries``
            # collection is loaded via ``selectinload``, which doesn't promise any particular order.
            fc_ids = sorted(fe.flashcard_id for fe in entry.flashcard_entries)
            fc_str = ", ".join(str(i) for i in fc_ids) if fc_ids else "—"
            cells = (
                Text(marker, style=style),
                Text(str(entry.id), style=style),
                Text(entry.title, style=style),
                Text(type_str, style=style),
                topic_cell,
                Text(fc_str, style=style),
            )
            if path == "inplace":
                for col, value in enumerate(cells):
                    table.update_cell_at(Coordinate(i, col), value)
            else:
                table.add_row(*cells, key=str(entry.id))
        self._last_row_signature = new_signature

        # After a rebuild, ``table.clear()`` reset the table cursor to row 0. Push the VM's cursor
        # back into the table so the highlight lands on the row the VM expects. ``move_cursor``
        # fires ``RowHighlighted``, which round-trips into ``vm.set_cursor`` — the
        # early-return-on-equality there keeps this from looping. On ``extend`` / ``inplace`` the
        # cursor was never disturbed, so we skip.
        if (
            path == "rebuild"
            and self._vm.entries
            and 0 <= self._vm.cursor < len(self._vm.entries)
        ):
            table.move_cursor(row=self._vm.cursor, animate=False)

        status = self.query_one("#pane-status", Static)
        status.update(self._format_status())

    # ------------------------------------------------------------------
    # Cross-region focus (driven by ``BrowserView``'s alt+left/right)
    # ------------------------------------------------------------------
    #
    # Two regions at this level: the entries table and the details panel. The details panel has its
    # own internal cycle (title → content → choices) which we delegate to ``EntryDetailsView``. The
    # bool returns let the ``BrowserView`` know when the pane is at its leftmost edge so it can roll
    # focus back to the tree.

    def focus_first(self) -> None:
        """Entry point when ``BrowserView`` enters the pane from the tree. Land on the active
        dialog if there is one (so the user picks up where they left off after a tree side-trip),
        else the entries table."""
        if self._active_dialog is not None:
            try:
                widget_id = dict(self._DIALOG_WIDGETS)[self._active_dialog]
                self.query_one(widget_id, Static).focus()
                return
            except Exception:
                pass
        self.query_one("#entries-table", DataTable).focus()

    def focus_next_region(self) -> bool:
        focused = self.screen.focused if self.screen else None
        table = self.query_one("#entries-table", DataTable)
        details = self.query_one(EntryDetailsView)
        linked = self.query_one(LinkedFlashcardsPaneView)
        # The right-hand region depends on state: details in ENTRIES, linked-flashcards table in
        # LINKED_FLASHCARDS.
        right = (
            linked
            if self._vm.state is self._vm.State.LINKED_FLASHCARDS
            else details
        )
        if focused is table:
            # In ``ENTRIES`` + multi-select, the details panel is frozen and has no useful edit
            # affordances — short-circuit so ``alt+right`` keeps the user on the table.
            if (
                self._vm.state is self._vm.State.ENTRIES
                and self._vm.multi_select_active
            ):
                return False
            right.focus_first()
            return True
        if focused is not None and right in focused.ancestors_with_self:
            return right.focus_next_region()
        # Defensive fallback: focus was somewhere unexpected. Start the cycle from the leftmost
        # region.
        self.focus_first()
        return True

    def focus_prev_region(self) -> bool:
        focused = self.screen.focused if self.screen else None
        table = self.query_one("#entries-table", DataTable)
        details = self.query_one(EntryDetailsView)
        linked = self.query_one(LinkedFlashcardsPaneView)
        right = (
            linked
            if self._vm.state is self._vm.State.LINKED_FLASHCARDS
            else details
        )
        if focused is table:
            # Pane's leftmost edge — let ``BrowserView`` hand focus to the tree.
            return False
        if focused is not None and right in focused.ancestors_with_self:
            moved = right.focus_prev_region()
            if not moved:
                table.focus()
            return True
        return False

    # ------------------------------------------------------------------
    # Edit-dialog choice dispatch
    # ------------------------------------------------------------------
    #
    # Called from ``_EditBar.action_select`` with the chosen option string. Two of the choices
    # (``change topic`` / ``change type``) open modal screens; ``edit title`` / ``edit content``
    # focus a TextArea in the details panel; ``delete`` swaps to the delete confirm dialog.

    async def handle_edit_choice(self, choice: str) -> None:
        if choice == "change topic":
            await self._dispatch_change_topic()
        elif choice == "change type":
            await self._dispatch_change_type()
        elif choice == "edit title":
            self._dispatch_focus_details_field("details-title")
        elif choice == "edit content":
            self._dispatch_focus_details_field("details-content")
        elif choice == "delete":
            self.show_dialog("delete")

    async def _dispatch_change_topic(self) -> None:
        """Open ``TopicSelectorScreen``; on dismiss apply the choice via the VM. No-op if there's
        nothing to act on (empty selection or empty window)."""
        # Local import to avoid circulating tui.screens through the widget module at import time —
        # matches the pattern already used in commit_proposal/view.py.
        from rhizome.tui.screens.topic_selector import TopicSelectorScreen

        if self.selection_target_count() == 0:
            return

        def on_dismiss(result: tuple[int, str] | None) -> None:
            if result is None:
                # User cancelled — keep the edit bar open so they can pick a different action.
                try:
                    self.query_one("#edit-bar", _EditBar).focus()
                except Exception:
                    pass
                return
            topic_id, _ = result
            self.run_worker(
                self._vm.change_topic_on_selected_entries(topic_id), exclusive=False,
            )

        self.app.push_screen(
            TopicSelectorScreen(session_factory=self._vm.session_factory),
            callback=on_dismiss,
        )

    async def _dispatch_change_type(self) -> None:
        """Open the inline ``_TypePickerScreen``; on dismiss apply via the VM. Lands the modal's
        cursor on the cursor entry's current type (single-select only — multi-select has no single
        "current" to land on)."""
        if self.selection_target_count() == 0:
            return

        current: EntryType | None = None
        if not self._vm.multi_select_active and self._vm.entries:
            current = self._vm.entries[self._vm.cursor].entry_type

        def on_dismiss(result: EntryType | None) -> None:
            if result is None:
                try:
                    self.query_one("#edit-bar", _EditBar).focus()
                except Exception:
                    pass
                return
            self.run_worker(
                self._vm.change_type_on_selected_entries(result), exclusive=False,
            )

        self.app.push_screen(_TypePickerScreen(current=current), callback=on_dismiss)

    def _dispatch_focus_details_field(self, widget_id: str) -> None:
        """Edit title / edit content: dismiss the edit bar and focus the target TextArea in the
        details panel. Single-select only — the option list excludes these in multi-select mode."""
        self.hide_dialog()
        try:
            target = self.query_one(f"#{widget_id}", TextArea)
        except Exception:
            return
        target.focus()

    # ------------------------------------------------------------------
    # View → VM
    # ------------------------------------------------------------------

    def on_data_table_row_highlighted(
        self,
        event: DataTable.RowHighlighted,
    ) -> None:
        """Table cursor moved — push the row index into the VM.

        The VM's ``set_cursor`` no-ops if the index is unchanged, so this is safe to fire from
        programmatic ``move_cursor`` calls during ``_refresh`` (and from the initial mount, where
        the table seeds its cursor to row 0).
        """
        if event.data_table.id != "entries-table":
            return
        self._vm.set_cursor(event.cursor_row)

    def _format_status(self) -> str:
        if self._vm.is_loading:
            return "loading…"
        if self._vm.multi_select_active:
            # Multi-select takes over the status line — the "N of M" hint is still useful but
            # secondary, so we lead with the selection count.
            count = len(self._vm.selected_ids)
            noun = "entry" if count == 1 else "entries"
            return f"multi-select: {count} {noun} selected (m to exit, space to toggle)"
        total = self._vm.total
        loaded = len(self._vm.entries)
        if total is None:
            if loaded == 0:
                return "no entries"
            return f"{loaded} loaded"
        if loaded < total:
            return f"showing {loaded} of {total}"
        if total == 0:
            return "no entries"
        if total == 1:
            return "1 entry"
        return f"{total} entries"
