"""KnowledgeEntryBrowserPaneView — DataTable + details + status row.

See ``view_model.py`` for the VM contract and ``entry_details/`` for the side panel.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Static, TextArea

from rhizome.db.models import EntryType

from .entry_details import EntryDetailsView
from .view_model import (
    SORT_OPTIONS,
    KnowledgeEntryBrowserPaneViewModel,
    MultiSelectFilterViewModel,
)


class _EntriesTable(DataTable):
    """``DataTable`` subclass that owns the multi-select keybindings.

    Lives here rather than as standalone bindings on the parent view so the keys only fire when the table
    is focused — ``m`` and ``space`` on the details panel's ``TextArea``s would otherwise have to be
    suppressed. Both actions delegate straight to the pane VM; the table widget holds no state of its own.
    """

    BINDINGS = [
        Binding("m", "toggle_multi_select", show=False),
        Binding("space", "toggle_selection", show=False),
        # ``shift+up`` / ``shift+down`` are range-select sugar: add the cursor row to the selection
        # (idempotent) and step the cursor in one keystroke. Held-key terminal repeat makes "hold shift,
        # hold down" sweep a contiguous block. No-op outside multi-select (the VM guards). Bound here
        # rather than as ``"shift+up,shift+down"`` action pairs because each direction needs its own
        # cursor step.
        Binding("shift+down", "select_down", show=False),
        Binding("shift+up", "select_up", show=False),
        # ``d`` requests the delete confirm. Targets the selection in multi-select mode and the cursor
        # entry in single-select mode; the VM guards "nothing to delete" in both.
        Binding("d", "request_delete", show=False),
        # ``s`` opens the sort dialog. Available in both regular and multi-select mode (the VM clears any
        # selection when the sort is applied — see the dialog warning).
        Binding("s", "request_sort", show=False),
        # ``f`` opens the filter dialog. Mutually exclusive with sort / edit / delete (the VM cancels
        # whichever is open when ``f`` lands).
        Binding("f", "request_filter", show=False),
        # ``e`` opens the edit dialog. Same mutex membership as the others.
        Binding("e", "request_edit", show=False),
    ]

    def __init__(
        self,
        view_model: KnowledgeEntryBrowserPaneViewModel,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model

    def action_toggle_multi_select(self) -> None:
        self._vm.toggle_multi_select()

    def action_toggle_selection(self) -> None:
        self._vm.toggle_current_selection()

    async def action_select_down(self) -> None:
        """Add the current row to the selection, then step the cursor down. Cursor step uses
        ``action_cursor_down`` (our overridden async version, which also handles the load-more-at-bottom
        case) so the usual ``RowHighlighted`` event fires and the VM cursor stays in sync."""
        self._vm.add_current_to_selection()
        await self.action_cursor_down()

    def action_select_up(self) -> None:
        self._vm.add_current_to_selection()
        self.action_cursor_up()

    async def action_cursor_down(self) -> None:
        """Cursor-down with auto-load at the bottom edge: if the user is on the last loaded row and the VM
        still has more to fetch, await ``load_more`` first so the next ``super().action_cursor_down`` has
        somewhere to land. ``load_more`` is a no-op when nothing further is available or a fetch is
        already in flight, so this is safe to call without re-checking those conditions.

        The cursor advance happens *after* the await — by then the VM has appended rows + emitted dirty,
        ``_refresh`` ran in ``extend`` mode, and the table has the new rows mounted.
        """
        if (
            self._vm.has_more
            and self.row_count > 0
            and self.cursor_row >= self.row_count - 1
        ):
            await self._vm.load_more()
        super().action_cursor_down()

    def action_request_delete(self) -> None:
        self._vm.request_delete()

    def action_request_sort(self) -> None:
        self._vm.request_sort()

    def action_request_filter(self) -> None:
        self._vm.request_filter()

    def action_request_edit(self) -> None:
        self._vm.request_edit()


class _SearchInput(Input):
    """Search box mounted above the entries table.

    Visually mirrors the entry-detail title field: 3-row tight box, transparent background, ``#3a3a3a``
    border that flips accent on focus. The keybinding hint rides the top border on the right
    (``border_title`` + ``border_title_align = "right"``) — same space the dialogs use for their hint
    lines, but here we save a full row by fusing it into the border.

    Input state:
      * ``enter`` — submit current buffer to ``vm.set_search``.
      * ``esc`` × 2 — clear buffer + submit empty query (reset). The first esc arms; the second clears.
        Any non-``esc`` key disarms, so a stray esc followed by editing doesn't leave the next esc as a
        surprise nuke.

    The state machine lives here (rather than on a parent wrapper) because ``Input`` consumes character
    keystrokes before they bubble, so a parent ``on_key`` would never see "user typed something" — the
    signal we need to disarm.
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
        """Disarm on any non-escape key. Runs before the binding dispatch (so escape's own action still
        fires) and before ``Input``'s default character-insertion handling (so editing still works
        untouched)."""
        if event.key != "escape" and self.armed_for_clear:
            self.armed_for_clear = False
            self._refresh_title()

    def _refresh_title(self) -> None:
        if self.armed_for_clear:
            self.border_title = "[bold #ff8787]press esc again to clear[/]"
        else:
            self.border_title = "[dim]enter to submit • esc × 2 to clear[/]"


class _DeleteConfirm(Static, can_focus=True):
    """Delete confirmation dialog. Targets the multi-select selection or the single-select cursor entry
    depending on mode (the VM resolves this via ``delete_target_ids``).

    Mirrors ``_ChoicesList`` from ``entry_details/view.py`` — a focusable ``Static`` with up/down/enter
    bindings dispatching to the VM, plus ``escape`` for quick dismissal.

    Renders three lines: a header explaining the action (entry count + the no-flashcards-harmed promise),
    then two indented choice rows (Confirm / Cancel). Cursor brightness tracks focus, same as
    ``_ChoicesList``.
    """

    BINDINGS = [
        Binding("up", "choice_up", show=False),
        Binding("down", "choice_down", show=False),
        Binding("enter", "choice_confirm", show=False),
        Binding("escape", "cancel", show=False),
        # Mutex siblings: pressing one of these from inside the delete dialog dismisses delete and opens
        # the other. ``vm.request_*`` enforces the priority (each cancels delete before flipping itself
        # on).
        Binding("s", "request_sort", show=False),
        Binding("f", "request_filter", show=False),
        Binding("e", "request_edit", show=False),
    ]

    def __init__(
        self,
        view_model: KnowledgeEntryBrowserPaneViewModel,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    def on_focus(self) -> None:
        # Cursor brightness tracks focus — re-render on focus changes for the same reason
        # ``_ChoicesList`` does.
        self.call_after_refresh(self._refresh)

    def on_blur(self) -> None:
        self.call_after_refresh(self._refresh)

    def _refresh(self) -> None:
        # Note: not ``_render`` — that's a Textual-internal name (the widget's own ``_render`` returns
        # the cached Visual). Naming this method ``_render`` shadows the framework hook and Textual
        # tries to use the returned ``rich.text.Text`` as a ``Visual``, blowing up in ``to_strips``.
        self.update(self._render_dialog())

    def _render_dialog(self) -> Text:
        count = len(self._vm.delete_target_ids)
        noun = "entry" if count == 1 else "entries"
        # In single-select mode the lead-in is just "Delete this entry?" — "selected" reads weird when
        # there's no visible selection mark. Multi-select keeps the existing phrasing.
        scope_word = "selected " if self._vm.multi_select_active else ""
        cursor_style = "bold" if self.has_focus else "#6a6a6a"
        text = Text()
        text.append(f"Delete {count} {scope_word}{noun}? ", style="bold")
        text.append(
            "Linked flashcards will not be affected.", style="dim",
        )
        text.append("\n")
        labels = ("Confirm", "Cancel")
        for i, label in enumerate(labels):
            chosen = i == self._vm.delete_choice_cursor
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
        self._vm.move_delete_cursor(-1)

    def action_choice_down(self) -> None:
        self._vm.move_delete_cursor(1)

    async def action_choice_confirm(self) -> None:
        if self._vm.delete_choice_cursor == 0:
            await self._vm.confirm_delete()
        else:
            self._vm.cancel_delete()

    def action_cancel(self) -> None:
        self._vm.cancel_delete()

    def action_request_sort(self) -> None:
        self._vm.request_sort()

    def action_request_filter(self) -> None:
        self._vm.request_filter()

    def action_request_edit(self) -> None:
        self._vm.request_edit()


class _SortBar(Static, can_focus=True):
    """Sort-axis picker dialog. Sits in the same screen slot as ``_DeleteConfirm`` — only one is ever
    visible at a time, and the VM enforces priority (``request_sort`` cancels any pending delete).

    Renders horizontally, mirroring the data table's column order: ``id   title   type   topic``. The
    active sort is decorated with an arrow (``↑`` / ``↓``) and brackets; the cursor option is shown in a
    bold accent colour (no ``►`` prefix — keeping the row at a fixed width avoids the option labels
    jumping around as the cursor moves). A second line carries a help hint, extended with a
    selection-clearing warning while multi-select is on.

    Keys: ``left`` / ``right`` move the cursor (with wrap); ``enter`` applies (toggles direction when on
    the active axis, otherwise switches to that axis ascending); ``s`` and ``escape`` dismiss without
    applying.
    """

    BINDINGS = [
        Binding("left", "cursor_left", show=False),
        Binding("right", "cursor_right", show=False),
        Binding("enter", "apply", show=False),
        # ``r`` resets to the default sort (``id`` ascending). Mirrors the same key in the filter dialog.
        Binding("r", "reset", show=False),
        # ``s`` toggles the dialog closed — symmetric with the ``s``-opens-it binding on
        # ``_EntriesTable``.
        Binding("s", "cancel", show=False),
        # ``f`` swaps to the filter dialog. ``request_filter`` on the VM dismisses sort first so the two
        # never co-exist.
        Binding("f", "request_filter", show=False),
        # ``e`` swaps to the edit dialog. Same mutex shape.
        Binding("e", "request_edit", show=False),
        Binding("escape", "cancel", show=False),
    ]

    def __init__(
        self,
        view_model: KnowledgeEntryBrowserPaneViewModel,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    def on_focus(self) -> None:
        # Cursor colour brightens on focus, same convention as the ``_ChoicesList`` / ``_DeleteConfirm``
        # widgets.
        self.call_after_refresh(self._refresh)

    def on_blur(self) -> None:
        self.call_after_refresh(self._refresh)

    def _refresh(self) -> None:
        self.update(self._render_bar())

    def _render_bar(self) -> Text:
        active_idx = (
            SORT_OPTIONS.index(self._vm.sort_by)
            if self._vm.sort_by in SORT_OPTIONS
            else -1
        )
        arrow = "↑" if self._vm.sort_dir == "asc" else "↓"
        # Cursor colour: bright gold on focus, dim grey otherwise. The active axis itself always renders
        # in the default fg so the arrow + brackets carry the "this is the live sort" signal.
        cursor_color = "bold #ffd700" if self.has_focus else "bold #6a6a6a"

        text = Text()
        for i, option in enumerate(SORT_OPTIONS):
            is_active = i == active_idx
            is_cursor = i == self._vm.sort_cursor
            label = f"{arrow}[{option}]" if is_active else option
            if is_cursor:
                style = cursor_color
            elif is_active:
                style = ""  # default fg
            else:
                style = "#787878"
            text.append(label, style=style)
            if i < len(SORT_OPTIONS) - 1:
                text.append("   ")
        text.append("\n")

        # Help line — extended with the selection-clearing warning when in multi-select. The warning
        # sits inline rather than on a third line so the dialog can stay at a fixed 4-line height across
        # both modes.
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
        self._vm.move_sort_cursor(-1)

    def action_cursor_right(self) -> None:
        self._vm.move_sort_cursor(1)

    def action_apply(self) -> None:
        self._vm.apply_sort()

    def action_reset(self) -> None:
        self._vm.reset_sort()

    def action_cancel(self) -> None:
        self._vm.cancel_sort()

    def action_request_filter(self) -> None:
        self._vm.request_filter()

    def action_request_edit(self) -> None:
        self._vm.request_edit()


class _FilterDialog(Static, can_focus=True):
    """Per-axis filter picker. Shares the same screen slot as ``_SortBar`` and ``_DeleteConfirm`` (the
    three are mutually exclusive at the VM level).

    The widget is built to accept multiple filter "categories" — each a ``FilterCategoryViewModel``
    subclass — even though the pane currently only carries one (type). The top line shows the category
    tabs; underneath sits whatever input shape the active category needs. Rendering and key handling
    dispatch on the concrete category type (currently just ``MultiSelectFilterViewModel``): adding a new
    category type means a new subclass plus one new branch in both ``_render_active_category`` and the
    keystroke handlers.

    Keys:
      * ``tab`` / ``shift+tab`` — cycle categories (no-op with one)
      * ``left`` / ``right`` — move the cursor within the active category
      * ``space`` — toggle the cursor's option (MultiSelect)
      * ``r`` — reset every category to default
      * ``s`` — swap to the sort dialog
      * ``f`` / ``escape`` — dismiss
    """

    BINDINGS = [
        Binding("tab", "cycle_category(1)", show=False),
        Binding("shift+tab", "cycle_category(-1)", show=False),
        Binding("left", "cursor_left", show=False),
        Binding("right", "cursor_right", show=False),
        Binding("space", "toggle", show=False),
        Binding("r", "reset", show=False),
        Binding("s", "request_sort", show=False),
        # ``f`` toggles the dialog closed (symmetric with the ``f``-opens-it binding on
        # ``_EntriesTable``).
        Binding("f", "cancel", show=False),
        # ``e`` swaps to the edit dialog.
        Binding("e", "request_edit", show=False),
        Binding("escape", "cancel", show=False),
    ]

    def __init__(
        self,
        view_model: KnowledgeEntryBrowserPaneViewModel,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    def on_focus(self) -> None:
        # Cursor colours brighten on focus, matching the other dialogs.
        self.call_after_refresh(self._refresh)

    def on_blur(self) -> None:
        self.call_after_refresh(self._refresh)

    def _refresh(self) -> None:
        self.update(self._render_dialog())

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render_dialog(self) -> Text:
        categories = self._vm.filter_categories
        active = self._vm.filter_active_category

        text = Text()
        # Line 1 — category tabs. The active tab gets brackets; non-default categories pick up a green
        # tint so the user can see at a glance which filters are currently narrowing the view.
        text.append("filter by:  ", style="dim")
        for i, cat in enumerate(categories):
            is_active = active is cat
            colour = "#5fd75f" if not cat.is_default else ""
            if is_active:
                text.append("[", style=colour or "")
                text.append(cat.name, style=("bold " + colour).strip())
                text.append("]", style=colour or "")
            else:
                text.append(cat.name, style=colour or "#787878")
            if i < len(categories) - 1:
                text.append("   ")
        text.append("\n")

        # Line 2 — active category body. Dispatch on category type.
        if active is not None:
            text.append_text(self._render_active_category(active))
        text.append("\n")

        # Line 3 — hint, extended with the selection-clearing warning while multi-select is on (mirrors
        # ``_SortBar``'s pattern).
        hint = Text()
        bits = []
        if len(categories) > 1:
            bits.append("tab switch")
        bits.append("← / → move")
        if isinstance(active, MultiSelectFilterViewModel):
            bits.append("space toggle")
        bits.append("r reset")
        bits.append("s sort")
        bits.append("f/esc dismiss")
        hint.append(" • ".join(bits), style="dim")
        if self._vm.multi_select_active:
            hint.append("   ", style="dim")
            hint.append("Toggling clears your selection.", style="#ff8787")
        text.append(hint)
        return text

    def _render_active_category(self, category) -> Text:
        if isinstance(category, MultiSelectFilterViewModel):
            return self._render_multiselect(category)
        # Defensive: unknown category type — paint a placeholder so we don't blow up rendering. Concrete
        # handling lands when a new subclass is added.
        return Text(f"(no renderer for {type(category).__name__})", style="dim")

    def _render_multiselect(self, category: MultiSelectFilterViewModel) -> Text:
        # Cursor colour: bright gold on focus, dim otherwise. Same convention as ``_SortBar``.
        cursor_color = "bold #ffd700" if self.has_focus else "bold #6a6a6a"
        text = Text()
        for i, option in enumerate(category.options):
            is_cursor = i == category.cursor
            is_sel = category.is_selected(option)
            marker = "[x]" if is_sel else "[ ]"
            marker_style = "#5fd75f" if is_sel else "#787878"
            label_style = cursor_color if is_cursor else ""
            text.append(marker, style=marker_style)
            text.append(" ")
            text.append(option, style=label_style)
            if i < len(category.options) - 1:
                text.append("    ")
        return text

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_cycle_category(self, direction: int) -> None:
        self._vm.filter_tab(direction)

    def action_cursor_left(self) -> None:
        self._vm.filter_move_cursor(-1)

    def action_cursor_right(self) -> None:
        self._vm.filter_move_cursor(1)

    def action_toggle(self) -> None:
        self._vm.filter_toggle_current()

    def action_reset(self) -> None:
        self._vm.filter_reset()

    def action_cancel(self) -> None:
        self._vm.cancel_filter()

    def action_request_sort(self) -> None:
        self._vm.request_sort()

    def action_request_edit(self) -> None:
        self._vm.request_edit()


class _TypePickerScreen(ModalScreen[EntryType | None]):
    """Modal screen for picking an ``EntryType``. Three options laid out vertically; arrows / enter /
    escape. Dismisses with the chosen ``EntryType`` (caller applies it) or ``None`` on cancel.

    Deliberately co-located with the pane view rather than under ``tui/screens/`` — the picker is tiny
    and only used here, so the extra indirection isn't worth it (matching the brief). If a second
    consumer shows up, lift it out.
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
        # Land the cursor on the current type when there is one, so the most common "I want to change
        # to something other than this" flow is one ``down`` away.
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
    """Edit-action picker. Sits in the same screen slot as the sort / filter / delete dialogs (mutually
    exclusive at the VM level via the four-way mutex).

    Renders horizontally: option list on one line, hint on the next. Options come from
    ``vm.edit_options`` (mode-dependent — multi-select hides the per-entry edit shortcuts).

    Keys: ``left`` / ``right`` move the cursor (wrap); ``enter`` dispatches the highlighted choice;
    ``e`` / ``escape`` dismiss; ``s`` / ``f`` / ``d`` swap to the corresponding sibling dialog.

    Dispatch sits here (not on the VM) because two of the choices — ``change topic`` and ``change
    type`` — open modal screens, which is a view-side concern; and the other two (``edit title`` /
    ``edit content``) are pure focus shortcuts to the details panel. Only the bookkeeping for which
    choice was picked needs to round-trip through the VM, and that's already covered by the existing
    ``edit_cursor`` state.
    """

    BINDINGS = [
        Binding("left", "cursor_left", show=False),
        Binding("right", "cursor_right", show=False),
        Binding("enter", "select", show=False),
        # ``e`` toggles the dialog closed (symmetric with ``e`` on ``_EntriesTable``).
        Binding("e", "cancel", show=False),
        Binding("escape", "cancel", show=False),
        Binding("s", "request_sort", show=False),
        Binding("f", "request_filter", show=False),
        Binding("d", "request_delete", show=False),
    ]

    def __init__(
        self,
        view_model: KnowledgeEntryBrowserPaneViewModel,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    def on_focus(self) -> None:
        # Cursor colour brightens on focus — same convention as the other dialogs.
        self.call_after_refresh(self._refresh)

    def on_blur(self) -> None:
        self.call_after_refresh(self._refresh)

    def _refresh(self) -> None:
        self.update(self._render_bar())

    def _render_bar(self) -> Text:
        cursor_color = "bold #ffd700" if self.has_focus else "bold #6a6a6a"
        text = Text()
        # Lead-in: "edit N entries:" / "edit this entry:" — gives the user a clear scope reminder while
        # they navigate.
        targets = self._vm.edit_target_ids()
        count = len(targets)
        if self._vm.multi_select_active:
            noun = "entry" if count == 1 else "entries"
            text.append(f"edit {count} {noun}:  ", style="dim")
        else:
            text.append("edit this entry:  ", style="dim")
        options = self._vm.edit_options
        for i, opt in enumerate(options):
            is_cursor = i == self._vm.edit_cursor
            style = cursor_color if is_cursor else "#787878"
            text.append(opt, style=style)
            if i < len(options) - 1:
                text.append("   ")
        text.append("\n")
        text.append("← / → move • enter select • e/esc dismiss", style="dim")
        return text

    def action_cursor_left(self) -> None:
        self._vm.move_edit_cursor(-1)

    def action_cursor_right(self) -> None:
        self._vm.move_edit_cursor(1)

    async def action_select(self) -> None:
        """Dispatch the highlighted choice. The parent pane view exposes the high-level handlers
        (``handle_edit_choice``) because two of them need access to the screen (push modal, refocus a
        TextArea inside another sibling widget). The bar's only job is to forward the cursor index."""
        pane = self._find_pane()
        if pane is None:
            return
        await pane.handle_edit_choice(self._vm.edit_cursor)

    def _find_pane(self) -> KnowledgeEntryBrowserPaneView | None:
        """Walk up to the enclosing pane view. Done at action time (not on mount) so we don't take a
        hard reference to the parent that would survive remount."""
        node = self.parent
        while node is not None:
            if isinstance(node, KnowledgeEntryBrowserPaneView):
                return node
            node = node.parent
        return None

    def action_cancel(self) -> None:
        self._vm.cancel_edit()

    def action_request_sort(self) -> None:
        self._vm.request_sort()

    def action_request_filter(self) -> None:
        self._vm.request_filter()

    def action_request_delete(self) -> None:
        self._vm.request_delete()


class KnowledgeEntryBrowserPaneView(Vertical):
    """Minimal view for ``KnowledgeEntryBrowserPaneViewModel``: a DataTable plus a one-line status row
    beneath. No detail panel, no search bar — those are explicitly out of scope for the first cut (see
    the braindump and the agreed iteration plan).

    Columns: id / title / type / topic_id. Title is truncated at render time (column width is bounded by
    the DataTable's auto-layout). Type renders as the enum value string, or ``—`` for entries with no
    type set.
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
       table. The 60% width that used to live on ``#entries-table``
       moved up to this container, so the table fills its parent.  */
    KnowledgeEntryBrowserPaneView #table-column {
        width: 60%;
        height: 1fr;
        layout: vertical;
    }
    KnowledgeEntryBrowserPaneView #entries-table {
        width: 1fr;
        height: 1fr;
        margin: 1 0 0 0;
    }
    /* Multi-select wash: keep the zebra alternation but shift both rows
       darker, so the table reads as muted-but-structured and the bright-
       green selected rows pop. ``$surface-darken-2`` is the odd-row
       (table-base) colour; even rows sit one step above that, mirroring
       the regular-mode relative offset at a darker absolute level. */
    KnowledgeEntryBrowserPaneView #entries-table.-multi-select {
        background: $surface-darken-2;
    }
    KnowledgeEntryBrowserPaneView #entries-table.-multi-select > .datatable--even-row {
        background: $surface-darken-1 50%;
    }
    KnowledgeEntryBrowserPaneView EntryDetailsView {
        width: 40%;
        height: 1fr;
    }
    KnowledgeEntryBrowserPaneView #pane-status {
        dock: bottom;
        height: 1;
        color: $foreground-muted;
        text-style: dim;
        padding: 0 1;
    }
    KnowledgeEntryBrowserPaneView #delete-confirm {
        /* 3 lines of content (header + Confirm + Cancel) plus the
           ``border-top`` itself, which counts toward the box height. */
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
        /* 2 lines of content (options + hint) plus the ``border-top``. */
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
        /* 3 lines of content (tabs + body + hint) plus the
           ``border-top``. */
        height: 4;
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
        /* 2 lines of content (options + hint) plus the ``border-top``. */
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

    def __init__(
        self,
        view_model: KnowledgeEntryBrowserPaneViewModel,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model
        # Tracks the previous ``delete_pending`` so ``_refresh`` can detect the open / close transition
        # and grab / restore focus. Without this, opening the dialog wouldn't auto-focus it (forcing the
        # user to alt-tab around), and closing it would leave focus on a ``display: none`` widget.
        self._was_delete_pending: bool = False
        # Same edge-detection pattern for the sort dialog. See the ``_was_delete_pending`` note above
        # for the rationale.
        self._was_sort_pending: bool = False
        # And the filter dialog.
        self._was_filter_pending: bool = False
        # And the edit bar.
        self._was_edit_pending: bool = False
        # Signature of the entries list at the last refresh — a tuple of entry ids in display order.
        # Used by ``_refresh`` to decide between a full ``clear()`` + rebuild (when row identity has
        # actually changed: refetch, delete, load_more) and a cheap in-place ``update_cell_at`` pass
        # (when only styles or markers changed: mode toggle, selection toggle, post-edit content
        # mutation). The in-place path preserves ``DataTable``'s scroll position and cursor — without
        # it, every selection toggle resets scroll to 0 and the auto-re-scroll lands the cursor row at
        # the bottom of the viewport instead of leaving it where the user had it. ``None`` forces the
        # first refresh through the rebuild path (the table is empty then anyway).
        self._last_row_signature: tuple[int, ...] | None = None

    # Max display width for the title column. Anything longer is truncated by ``DataTable`` (with an
    # ellipsis). 50 is an arbitrary first-cut tuned against the current sample data; lift if it ever
    # bites.
    _TITLE_COLUMN_WIDTH = 50

    def compose(self):
        table = _EntriesTable(
            self._vm, id="entries-table", cursor_type="row", zebra_stripes=True,
        )
        # ``key`` strings give us a stable per-row id so cursor restoration across reloads is possible
        # later if we want it. They're not used by the view today.
        # ``title`` is the only column with a fixed width — the rest auto-size to their content. Without
        # the cap, titles like the 67-character "Linear Algebra: Vector Spaces …" expand the column to
        # the full width of the longest title, squeezing everything else.
        #
        # The leading "sel" column is always present (we can't add or drop columns cleanly after
        # construction). When multi-select is off the column renders empty; when on, each row shows
        # ``[ ]`` or ``[x]``. Width 3 fits the marker glyph; DataTable's default cell padding takes care
        # of the breathing room.
        table.add_column("sel", width=3)
        table.add_column("id")
        table.add_column("title", width=self._TITLE_COLUMN_WIDTH)
        table.add_column("type")
        table.add_column("topic")
        with Horizontal(id="pane-body"):
            with Vertical(id="table-column"):
                yield _SearchInput(self._vm, id="search-input")
                yield table
            yield EntryDetailsView(self._vm.details)
        yield _DeleteConfirm(self._vm, id="delete-confirm")
        yield _SortBar(self._vm, id="sort-bar")
        yield _FilterDialog(self._vm, id="filter-dialog")
        yield _EditBar(self._vm, id="edit-bar")
        yield Static("", id="pane-status")

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        # If the VM already has data (it was bootstrapped before the view mounted), paint it on first
        # frame instead of waiting for the next dirty.
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    def _refresh(self) -> None:
        table = self.query_one("#entries-table", DataTable)
        mode = self._vm.multi_select_active
        # ``-multi-select`` triggers the CSS that darkens the zebra-row palette while the user is
        # picking.
        table.set_class(mode, "-multi-select")

        # Three refresh paths, picked by comparing the new id-tuple to the previously-rendered one:
        #
        #   * ``extend`` — old tuple is a prefix of the new one, length grew. ``load_more`` appends
        #     rows; we ``add_row`` only the new tail. No ``clear``, no cursor restore, scroll stays
        #     where the user left it.
        #   * ``in-place`` — same tuple. Pure style/marker churn (multi-select toggle, selection toggle,
        #     post-edit content mutation). ``update_cell_at`` per cell preserves scroll + cursor.
        #   * ``rebuild`` — anything else (refetch, delete, reorder). ``clear`` + ``add_row``, restore
        #     cursor.
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
            # Topic column shows the topic name followed by " [{id}]" in a fixed dim grey — matches the
            # topic tree's hint style. ``selectinload`` on ``list_entries_paginated`` ensures
            # ``entry.topic`` is populated before the session closes; the defensive fallback to
            # ``topic_id`` is here in case something ever lands an entry whose topic FK isn't loaded.
            topic_name = entry.topic.name if entry.topic is not None else "?"
            topic_cell = Text.assemble(
                (topic_name, style),
                (f" [{entry.topic_id}]", "#787878"),
            )
            cells = (
                Text(marker, style=style),
                Text(str(entry.id), style=style),
                Text(entry.title, style=style),
                Text(type_str, style=style),
                topic_cell,
            )
            if path == "inplace":
                # Overwrite each cell in row ``i``. Style is carried inside each ``Text`` value so this
                # picks up the new colours/bold for free.
                for col, value in enumerate(cells):
                    table.update_cell_at(Coordinate(i, col), value)
            else:
                # ``rebuild`` and ``extend`` both append via ``add_row``. The difference is whether
                # ``clear()`` ran above.
                table.add_row(*cells, key=str(entry.id))
        self._last_row_signature = new_signature

        # After a rebuild, ``table.clear()`` reset the table cursor to row 0. Push the VM's cursor back
        # into the table so the highlight lands on the row the VM expects. ``move_cursor`` fires
        # ``RowHighlighted``, which round-trips into ``vm.set_cursor`` — the early-return-on-equality
        # there keeps this from looping. On the ``extend`` and ``inplace`` paths the cursor was never
        # disturbed, so we skip this entirely.
        if (
            path == "rebuild"
            and self._vm.entries
            and 0 <= self._vm.cursor < len(self._vm.entries)
        ):
            table.move_cursor(row=self._vm.cursor, animate=False)

        status = self.query_one("#pane-status", Static)
        status.update(self._format_status())

        # Three-dialog visibility + coordinated focus rescue. The ``_was_*`` edge detectors mirror the
        # ``_was_dirty`` pattern in ``EntryDetailsView``; the open/close logic has to coordinate across
        # all three because a swap (e.g. ``s`` pressed while the filter dialog is open) closes one and
        # opens another in the *same* refresh — running each dialog's focus rescue independently lets
        # the closing one's "restore focus to table" overwrite the opening one's "focus dialog" grab.
        delete_dialog = self.query_one("#delete-confirm", _DeleteConfirm)
        sort_bar = self.query_one("#sort-bar", _SortBar)
        filter_dialog = self.query_one("#filter-dialog", _FilterDialog)
        edit_bar = self.query_one("#edit-bar", _EditBar)

        delete_pending = self._vm.delete_pending
        sort_pending = self._vm.sort_pending
        filter_pending = self._vm.filter_pending
        edit_pending = self._vm.edit_pending

        delete_dialog.set_class(delete_pending, "-visible")
        sort_bar.set_class(sort_pending, "-visible")
        filter_dialog.set_class(filter_pending, "-visible")
        edit_bar.set_class(edit_pending, "-visible")

        # Resolve focus once after all visibility flips are queued. Opens beat closes — if any dialog
        # just opened, grab focus to it (priority order: edit, filter, sort, delete, matching the VM's
        # mutual-exclusion preference). Otherwise, if at least one dialog just closed *and* nothing is
        # currently open, restore focus to the table.
        just_opened_edit = edit_pending and not self._was_edit_pending
        just_opened_filter = filter_pending and not self._was_filter_pending
        just_opened_sort = sort_pending and not self._was_sort_pending
        just_opened_delete = delete_pending and not self._was_delete_pending
        just_closed_any = (
            (self._was_delete_pending and not delete_pending)
            or (self._was_sort_pending and not sort_pending)
            or (self._was_filter_pending and not filter_pending)
            or (self._was_edit_pending and not edit_pending)
        )
        any_pending = delete_pending or sort_pending or filter_pending or edit_pending

        if just_opened_edit:
            edit_bar.focus()
        elif just_opened_filter:
            filter_dialog.focus()
        elif just_opened_sort:
            sort_bar.focus()
        elif just_opened_delete:
            delete_dialog.focus()
        elif just_closed_any and not any_pending:
            try:
                self.query_one("#entries-table", DataTable).focus()
            except Exception:
                # Table may have been unmounted (e.g. pane swap mid-close); let focus settle wherever
                # Textual puts it.
                pass

        self._was_delete_pending = delete_pending
        self._was_sort_pending = sort_pending
        self._was_filter_pending = filter_pending
        self._was_edit_pending = edit_pending

    # ------------------------------------------------------------------
    # Cross-region focus (driven by ``BrowserView``'s alt+left/right)
    # ------------------------------------------------------------------
    #
    # Two regions at this level: the entries table and the details panel. The details panel has its own
    # internal cycle (title → content → choices) which we delegate to ``EntryDetailsView``. The bool
    # returns let the ``BrowserView`` know when the pane is at its leftmost edge so it can roll focus
    # back to the tree.

    def focus_first(self) -> None:
        """Entry point when ``BrowserView`` enters the pane from the tree. Land on the leftmost
        focusable sub-region — normally the table, but if a dialog is open we re-focus it instead so the
        user picks up where they left off after a tree side-trip (alt+left from a dialog hops back to
        the tree). The three dialogs are mutually exclusive at the VM level so this order of checks only
        documents priority."""
        if self._vm.edit_pending:
            try:
                self.query_one("#edit-bar", _EditBar).focus()
                return
            except Exception:
                pass
        if self._vm.filter_pending:
            try:
                self.query_one("#filter-dialog", _FilterDialog).focus()
                return
            except Exception:
                pass
        if self._vm.sort_pending:
            try:
                self.query_one("#sort-bar", _SortBar).focus()
                return
            except Exception:
                pass
        if self._vm.delete_pending:
            try:
                self.query_one("#delete-confirm", _DeleteConfirm).focus()
                return
            except Exception:
                pass
        self.query_one("#entries-table", DataTable).focus()

    def focus_next_region(self) -> bool:
        focused = self.screen.focused if self.screen else None
        table = self.query_one("#entries-table", DataTable)
        details = self.query_one(EntryDetailsView)
        if focused is table:
            # While multi-select is on, the details panel is frozen and has no useful edit affordances —
            # short-circuit the transition so ``alt+right`` keeps the user on the table. Returning False
            # here lets ``BrowserView.action_focus_right`` treat the table as the rightmost edge.
            if self._vm.multi_select_active:
                return False
            details.focus_first()
            return True
        if focused is not None and details in focused.ancestors_with_self:
            return details.focus_next_region()
        # Defensive fallback: focus was somewhere unexpected inside the pane. Start the cycle from the
        # leftmost region.
        self.focus_first()
        return True

    def focus_prev_region(self) -> bool:
        focused = self.screen.focused if self.screen else None
        table = self.query_one("#entries-table", DataTable)
        details = self.query_one(EntryDetailsView)
        if focused is table:
            # Pane's leftmost edge — let ``BrowserView`` hand focus to the tree.
            return False
        if focused is not None and details in focused.ancestors_with_self:
            moved = details.focus_prev_region()
            if not moved:
                table.focus()
            return True
        return False

    # ------------------------------------------------------------------
    # Edit-dialog choice dispatch
    # ------------------------------------------------------------------
    #
    # Called from ``_EditBar.action_select``. The options list comes from ``vm.edit_options``; index 0
    # is always ``change topic``, index 1 ``change type``, index 2 (single only) ``edit title``, index
    # 3 (single only) ``edit content``, and the last entry is always ``delete``. We dispatch by the
    # option string rather than the numeric index so the multi/single shape difference can't go wrong.

    async def handle_edit_choice(self, cursor: int) -> None:
        options = self._vm.edit_options
        if cursor < 0 or cursor >= len(options):
            return
        choice = options[cursor]
        if choice == "change topic":
            await self._dispatch_change_topic()
        elif choice == "change type":
            await self._dispatch_change_type()
        elif choice == "edit title":
            self._dispatch_focus_details_field("details-title")
        elif choice == "edit content":
            self._dispatch_focus_details_field("details-content")
        elif choice == "delete":
            self._vm.request_delete()

    async def _dispatch_change_topic(self) -> None:
        """Open ``TopicSelectorScreen``; on dismiss apply the choice via the VM. ``edit_target_ids`` is
        evaluated *before* the screen pushes so single-select mode reads the cursor entry at the moment
        the user invoked the dialog (the VM has the same id frozen in
        ``_edit_single_target_id``)."""
        # Local import to avoid circulating tui.screens through the widget module at import time —
        # matches the pattern already used in commit_proposal/view.py.
        from rhizome.tui.screens.topic_selector import TopicSelectorScreen

        if not self._vm.edit_target_ids():
            return

        def on_dismiss(result: tuple[int, str] | None) -> None:
            if result is None:
                # User cancelled — keep the edit bar open so they can pick a different action without
                # re-pressing ``e``. Refocus the bar.
                try:
                    self.query_one("#edit-bar", _EditBar).focus()
                except Exception:
                    pass
                return
            topic_id, _ = result
            self.run_worker(self._vm.apply_change_topic(topic_id), exclusive=False)

        self.app.push_screen(
            TopicSelectorScreen(session_factory=self._vm.session_factory),
            callback=on_dismiss,
        )

    async def _dispatch_change_type(self) -> None:
        """Open the inline ``_TypePickerScreen``; on dismiss apply via the VM. Lands the modal's cursor
        on the cursor entry's current type (single-select only — multi-select has no single "current"
        to land on)."""
        if not self._vm.edit_target_ids():
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
            self.run_worker(self._vm.apply_change_type(result), exclusive=False)

        self.app.push_screen(_TypePickerScreen(current=current), callback=on_dismiss)

    def _dispatch_focus_details_field(self, widget_id: str) -> None:
        """Edit title / edit content: dismiss the edit bar and focus the target TextArea in the details
        panel. Single-select only — the VM's option list excludes these in multi-select mode."""
        self._vm.cancel_edit()
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
        programmatic ``move_cursor`` calls during ``_refresh`` (and from the initial mount, where the
        table seeds its cursor to row 0).
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
            # Window fetched but count not yet in — happens briefly between the two queries in
            # ``_fetch``.
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
