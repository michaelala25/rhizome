"""Knowledge-entry tab view. DataTable + right pane (details ↔ linked-flashcards) + bottom dialog
slot. Owns the dialog mutex, the global tab keys, and the cross-region focus graph.

Dialog mutex: a single ``_active_dialog`` slot toggled via ``show_dialog`` / ``hide_dialog`` /
``toggle_dialog``. Mutators flip the ``-visible`` class on the chosen widget, call its
``prepare_for_show()`` hook, and run focus rescue (focus the new dialog, or fall back to the
entries table on hide). State transitions auto-dismiss any open dialog.

Alt-arrow navigation: the tab owns its own ``alt+arrow`` bindings; each ``action_nav_<dir>``
resolves one step within the focus graph below via ``nav_<dir>``, and raises ``SkipAction``
when the step has no in-graph target (or returns the ``"topic_tree"`` sentinel) so the key
bubbles to ``Browser`` for the cross-region hop back to the panel.

The dialog node collapses all four dialogs to one ``"dialog"`` name.

| Node                        | Widget id                          | Present when                                                  |
|-----------------------------|------------------------------------|---------------------------------------------------------------|
| entry_search                | search-input                       | always                                                        |
| entry_table                 | entries-table                      | always                                                        |
| dialog                      | currently-shown dialog             | ``_active_dialog is not None``                                |
| entry_title                 | details-title                      | ``ENTRIES`` and not multi-select-frozen                       |
| entry_content               | details-content                    | ``ENTRIES`` and not multi-select-frozen                       |
| entry_modification_accept   | details-choices                    | ``ENTRIES``, not frozen, and ``details.is_dirty``             |
| flashcard_search            | linked-flashcards-search-input     | ``LINKED_FLASHCARDS``                                         |
| flashcard_table             | linked-flashcards-table            | ``LINKED_FLASHCARDS``                                         |
| relink_choices              | linked-flashcards-relink-choices   | ``LINKED_FLASHCARDS`` and ``linked_flashcards.is_relink_dirty`` |

Edges per direction (target gated on ``_node_present``; fall-throughs use ``or``):

- alt+up:    dialog→entry_table · entry_table→entry_search · flashcard_table→flashcard_search ·
             relink_choices→flashcard_table · entry_modification_accept→entry_content ·
             entry_content→entry_title · entry_title→entry_search
- alt+down:  entry_search→entry_table · flashcard_search→flashcard_table ·
             entry_table→dialog · flashcard_table→(relink_choices or dialog) ·
             relink_choices→dialog · entry_title→entry_content ·
             entry_content→(entry_modification_accept or dialog) ·
             entry_modification_accept→dialog
- alt+left:  entry_search/entry_table/dialog→"topic_tree" sentinel · entry_title→entry_search ·
             entry_content/entry_modification_accept→entry_table · flashcard_search→entry_search ·
             flashcard_table/relink_choices→entry_table
- alt+right: entry_search→entry_title (ENTRIES) / flashcard_search (LINKED_FLASHCARDS) ·
             entry_table→entry_content (ENTRIES) / flashcard_table (LINKED_FLASHCARDS)
"""

from __future__ import annotations

from typing import Any, Literal

from rich.text import Text
from textual.actions import SkipAction
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.widget import Widget
from textual.widgets import DataTable, Input, Rule, Static, TextArea

from rhizome.app.browser.tabs.entries.tab import EntryTabVM
from rhizome.db.models import EntryType
from rhizome.tui.widgets.browser.shared.search_bar import SearchBar
from rhizome.tui.widgets.browser.tabs.entries.delete import EntriesDeleteMenu
from rhizome.tui.widgets.browser.tabs.entries.details import EntryDetails
from rhizome.tui.widgets.browser.tabs.entries.edit import EditMenu
from rhizome.tui.widgets.browser.tabs.entries.entry_preview import EntryPreview
from rhizome.tui.widgets.browser.tabs.entries.entry_table import EntryTable
from rhizome.tui.widgets.browser.tabs.entries.filter import FilterMenu
from rhizome.tui.widgets.browser.tabs.entries.linked_flashcards_panel import LinkedFlashcardsPanel
from rhizome.tui.widgets.browser.tabs.entries.sort import EntriesSortMenu
from rhizome.tui.widgets.browser.tabs.entries.type_picker import TypePickerScreen

_DialogName = Literal["delete", "sort", "filter", "edit"]


class EntryTab(Vertical):
    """Tab container. See module docstring for the dialog mutex and focus-graph contracts."""

    DEFAULT_CSS = """
    EntryTab {
        height: 1fr;
        layout: vertical;
        padding: 0 1;
    }
    EntryTab #tab-body {
        layout: horizontal;
        height: 1fr;
    }
    EntryTab #tab-body-rule {
        margin: 0 0 0 0;
        color: #3a3a3a;
    }
    /* Left column width is set per-state (60% ENTRIES, 50% LINKED_FLASHCARDS); 1-char right
       margin so the rule + right pane don't sit flush against the table. */
    EntryTab #table-column {
        height: 1fr;
        layout: vertical;
        margin-right: 1;
    }
    EntryTab #entries-table {
        width: 1fr;
        height: 1fr;
        margin: 1 0 0 0;
    }
    /* Content preview only shows in LINKED_FLASHCARDS (the details panel covers it in ENTRIES). */
    EntryTab #entry-content-preview {
        display: none;
    }
    EntryTab.-state-linked-flashcards #entries-table {
        height: 2fr;
    }
    EntryTab.-state-linked-flashcards #entry-content-preview {
        display: block;
        width: 1fr;
        height: 1fr;
        margin: 1 0 0 0;
    }
    /* Multi-select wash: keep zebra alternation but shift both rows darker so selected (bright
       green) rows pop. */
    EntryTab #entries-table.-multi-select {
        background: $surface-darken-2;
    }
    EntryTab #entries-table.-multi-select > .datatable--even-row {
        background: $surface-darken-1 50%;
    }
    /* State-driven layout swap. Both right-hand views are mounted up front; the ``-state-*``
       class toggles which is visible. */
    EntryTab.-state-entries #table-column {
        width: 60%;
    }
    EntryTab.-state-entries EntryDetails {
        width: 1fr;
        height: 1fr;
        display: block;
    }
    EntryTab.-state-entries LinkedFlashcardsPanel {
        display: none;
    }
    EntryTab.-state-linked-flashcards #table-column {
        width: 50%;
    }
    EntryTab.-state-linked-flashcards LinkedFlashcardsPanel {
        width: 1fr;
        height: 1fr;
        display: block;
    }
    EntryTab.-state-linked-flashcards EntryDetails {
        display: none;
    }
    /* Status row sits in #table-column so it aligns with the linked-flashcards docked status. */
    EntryTab #tab-status {
        height: 1;
        color: $foreground-muted;
        text-style: dim;
        padding: 0 1;
    }
    EntryTab #delete-confirm {
        height: 4;
        margin: 1 0 0 0;
        padding: 0 1;
        border-top: solid #3a3a3a;
        color: rgb(200,200,200);
        display: none;
    }
    EntryTab #delete-confirm.-visible {
        display: block;
    }
    EntryTab #delete-confirm:focus {
        border-top: solid $accent;
    }
    EntryTab #sort-bar {
        height: 3;
        margin: 1 0 0 0;
        padding: 0 1;
        border-top: solid #3a3a3a;
        color: rgb(200,200,200);
        display: none;
    }
    EntryTab #sort-bar.-visible {
        display: block;
    }
    EntryTab #sort-bar:focus {
        border-top: solid $accent;
    }
    EntryTab #filter-dialog {
        height: 4;
        margin: 1 0 0 0;
        padding: 0 1;
        border-top: solid #3a3a3a;
        color: rgb(200,200,200);
        display: none;
    }
    EntryTab #filter-dialog.-visible {
        display: block;
    }
    EntryTab #filter-dialog:focus {
        border-top: solid $accent;
    }
    /* Filter dialog: type row · flashcard row (radios + compact One-of input) · hint row. */
    EntryTab #filter-dialog #type-row,
    EntryTab #filter-dialog #hint-row {
        height: 1;
        width: 1fr;
    }
    EntryTab #filter-dialog #flashcard-row {
        height: 1;
        width: 1fr;
    }
    EntryTab #filter-dialog #flashcard-radios {
        height: 1;
        width: auto;
    }
    EntryTab #filter-dialog #one-of-input {
        height: 1;
        width: 25;
        padding: 0;
        margin: 0;
        border: none;
        /* ``background-tint`` overlays rather than replacing so the dialog's own colour shows
           through; lightens the field even when unfocused. */
        background: transparent;
        background-tint: $foreground 8%;
    }
    EntryTab #filter-dialog #one-of-input:focus {
        background-tint: $foreground 15%;
    }
    EntryTab #filter-dialog #one-of-close-bracket {
        height: 1;
        width: 1;
    }
    EntryTab #edit-bar {
        height: 3;
        margin: 1 0 0 0;
        padding: 0 1;
        border-top: solid #3a3a3a;
        color: rgb(200,200,200);
        display: none;
    }
    EntryTab #edit-bar.-visible {
        display: block;
    }
    EntryTab #edit-bar:focus {
        border-top: solid $accent;
    }
    /* Permanent keybindings line + separator rule at the very bottom of the tab. */
    EntryTab #tab-keybindings-rule {
        margin: 1 0 0 0;
        color: #3a3a3a;
    }
    EntryTab #tab-keybindings {
        height: auto;
        text-align: left;
        padding: 0 1;
    }
    """

    # Title column width — DataTable truncates longer with an ellipsis.
    _TITLE_COLUMN_WIDTH = 50

    # Dialog-name → widget selector. Driven generically by ``_refresh_dialog_visibility``.
    _DIALOG_WIDGETS: tuple[tuple[_DialogName, str], ...] = (
        ("delete", "#delete-confirm"),
        ("sort", "#sort-bar"),
        ("filter", "#filter-dialog"),
        ("edit", "#edit-bar"),
    )

    # Global tab keys — bound here (not on EntryTable) so the linked-flashcards table picks them
    # up for free. Each open dialog's own ``d``/``s``/``f``/``e`` bindings still win while focused
    # (focused widget's bindings take precedence over an ancestor's).
    BINDINGS = [
        Binding("d", "tab_toggle_dialog('delete')", show=False),
        Binding("s", "tab_toggle_dialog('sort')", show=False),
        Binding("f", "tab_toggle_dialog('filter')", show=False),
        Binding("e", "tab_toggle_dialog('edit')", show=False),
        Binding("l", "tab_toggle_relink", show=False),
        Binding("m", "tab_toggle_multi_select", show=False),
        Binding("tab", "tab_cycle_mode", show=False),
        Binding("alt+left", "nav_left", show=False),
        Binding("alt+right", "nav_right", show=False),
        Binding("alt+up", "nav_up", show=False),
        Binding("alt+down", "nav_down", show=False),
    ]

    def __init__(
        self,
        view_model: EntryTabVM,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model
        # Dialog mutex — at most one visible. Mutators run through ``toggle_dialog`` /
        # ``hide_dialog`` so visibility, focus, and ``prepare_for_show`` stay coordinated.
        self._active_dialog: _DialogName | None = None
        # VM state at last refresh — used to auto-dismiss the dialog on transitions.
        self._last_state: EntryTabVM.State | None = None
        # Tuple of entry ids at last refresh — drives the three ``_refresh`` paths
        # (``extend`` / ``inplace`` / ``rebuild``). ``inplace`` and ``extend`` preserve DataTable's
        # scroll position and cursor.
        self._last_row_signature: tuple[int, ...] | None = None

    def compose(self):
        table = EntryTable(
            self._vm, self,
            id="entries-table", cursor_type="row", zebra_stripes=True,
        )
        # Leading "sel" column is always present (DataTable doesn't support clean post-construction
        # column add/drop). Empty cells outside multi-select; ``[ ]`` / ``[x]`` inside.
        table.add_column("sel", width=3)
        table.add_column("id")
        table.add_column("title", width=self._TITLE_COLUMN_WIDTH)
        table.add_column("type")
        table.add_column("topic")
        table.add_column("flashcards")
        with Horizontal(id="tab-body"):
            with Vertical(id="table-column"):
                yield SearchBar[EntryTabVM](
                    self._vm, id="search-input",
                )
                yield table
                yield EntryPreview(self._vm, id="entry-content-preview")
                yield Static("", id="tab-status")

            yield Rule(orientation="vertical", line_style="solid", id="tab-body-rule")
            # Both right-hand views mount up front; CSS ``-state-*`` flips visibility. Mounting
            # once avoids re-subscribing each child's vm.dirty on every state flip.
            yield EntryDetails(self._vm.details)
            yield LinkedFlashcardsPanel(self._vm.linked_flashcards)
        yield EntriesDeleteMenu(self._vm, self, id="delete-confirm")
        yield EntriesSortMenu(self._vm, on_close=self.hide_dialog, id="sort-bar")
        yield FilterMenu(self._vm, self, id="filter-dialog")
        yield EditMenu(self._vm, self, id="edit-bar")
        yield Rule(line_style="solid", id="tab-keybindings-rule")
        yield Static(self._keybindings_text(), id="tab-keybindings")

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.dirty, self._refresh)
        # Paint immediately if the VM was bootstrapped before mount.
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)

    # ------------------------------------------------------------------
    # Bottom keybindings line
    # ------------------------------------------------------------------

    def _keybindings_text(self) -> str:
        # Per-dialog keys (← / → / enter / esc inside an open dialog) are documented by the dialog
        # widgets themselves.
        rows = [
            ("e", "edit"),
            ("f", "filter"),
            ("s", "sort"),
            ("d", "delete"),
            ("l", "link flashcards"),
            ("m", "multi-select"),
            ("tab", "cycle mode"),
            ("alt+←↑→↓", "navigate"),
        ]
        return "   ".join(
            f"[#a0a0a0]{key}[/] [#707070]{label}[/]" for key, label in rows
        )

    # ------------------------------------------------------------------
    # Global tab actions (d / s / f / e / l / m / tab) — see ``BINDINGS``
    # ------------------------------------------------------------------
    #
    # Each gates on ``_typing_active`` so typing inside an editable field doesn't trip the binding.
    # Defensive — Input/TextArea consume printable keys themselves — but makes intent explicit.

    def _typing_active(self) -> bool:
        focused = self.screen.focused if self.screen else None
        return isinstance(focused, (Input, TextArea))

    def action_tab_toggle_dialog(self, name: str) -> None:
        if self._typing_active():
            return
        self.toggle_dialog(name)  # type: ignore[arg-type]

    def action_tab_toggle_relink(self) -> None:
        if self._typing_active():
            return
        self.toggle_relink_mode()

    def action_tab_toggle_multi_select(self) -> None:
        if self._typing_active():
            return
        self._vm.toggle_multi_select()

    def action_tab_cycle_mode(self) -> None:
        # Two-state toggle for now; replace with an explicit picker if a third state lands.
        if self._typing_active():
            return
        current = self._vm.state
        target = (
            self._vm.State.LINKED_FLASHCARDS
            if current is self._vm.State.ENTRIES
            else self._vm.State.ENTRIES
        )
        self._vm.transition_to(target)

    # ------------------------------------------------------------------
    # Dialog orchestration
    # ------------------------------------------------------------------

    def toggle_dialog(self, name: _DialogName) -> None:
        if self._active_dialog == name:
            self.hide_dialog()
        else:
            self.show_dialog(name)

    def show_dialog(self, name: _DialogName) -> None:
        if self._active_dialog == name:
            return
        self._active_dialog = name
        self._refresh_dialog_visibility()

    def hide_dialog(self) -> None:
        if self._active_dialog is None:
            return
        self._active_dialog = None
        self._refresh_dialog_visibility()

    def _refresh_dialog_visibility(self) -> None:
        # Toggle ``-visible`` on the active dialog (and clear it on the others), run each newly-
        # active widget's ``prepare_for_show`` hook, then focus it; fall back to the entries table
        # when nothing's active.
        for name, widget_id in self._DIALOG_WIDGETS:
            try:
                # Widget (not Static) because FilterMenu is a Vertical container.
                widget = self.query_one(widget_id, Widget)
            except Exception:
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
                pass

    # ------------------------------------------------------------------
    # Relink mode entry point
    # ------------------------------------------------------------------

    def toggle_relink_mode(self) -> None:
        # Wraps the VM's enter/exit; closes any open dialog first (mutex lives view-side).
        self.hide_dialog()
        if self._vm.linked_flashcards.relink_mode:
            self._vm.exit_relink_mode()
        else:
            self._vm.enter_relink_mode()

    # ------------------------------------------------------------------
    # Selection-target helper (used by dialog rendering and dispatchers)
    # ------------------------------------------------------------------

    def selection_target_count(self) -> int:
        """Count of entries a selected-action would act on: ``selected_ids`` in multi-select; the
        cursor entry (or zero if the window is empty) in single-select."""
        if self._vm.multi_select_active:
            return len(self._vm.selected_ids)
        return 1 if self._vm.entries else 0

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        table = self.query_one("#entries-table", DataTable)
        mode = self._vm.multi_select_active
        table.set_class(mode, "-multi-select")

        state = self._vm.state
        self.set_class(state is self._vm.State.ENTRIES, "-state-entries")
        self.set_class(state is self._vm.State.LINKED_FLASHCARDS, "-state-linked-flashcards")

        # State transitions auto-dismiss any open dialog — its targets/axes/options aren't generally
        # meaningful across a layout switch.
        if self._last_state is not None and state != self._last_state:
            if self._active_dialog is not None:
                self.hide_dialog()
        self._last_state = state

        # Three refresh paths:
        #   * ``extend`` — id-tuple grew with the old as prefix (``load_more``): add_row the tail.
        #   * ``inplace`` — same id-tuple (style/marker churn): update_cell_at, preserves cursor.
        #   * ``rebuild`` — anything else (refetch / delete / reorder): clear + add_row + restore.
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
            # Three colour regimes: non-multi (zebra), multi-non-selected (darker zebra),
            # multi-selected (bright green + bold).
            selected = mode and entry.id in self._vm.selected_ids
            if selected:
                style = "bold #5fd75f"
            elif mode:
                style = "#787878" if i % 2 else "#a0a0a0"
            else:
                style = "#a0a0a0" if i % 2 else ""
            marker = ("[x]" if selected else "[ ]") if mode else ""
            # Topic name + " [id]" in dim grey — matches the topic tree's hint style.
            topic_name = entry.topic.name if entry.topic is not None else "?"
            topic_cell = Text.assemble(
                (topic_name, style),
                (f" [{entry.topic_id}]", "#787878"),
            )
            # Sort flashcard ids for stable display — selectinload doesn't promise order.
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

        # Restore the VM cursor after rebuild (``clear`` reset it). ``move_cursor`` fires
        # RowHighlighted → vm.set_cursor; the VM's equality early-return kills the loop in one trip.
        if (
            path == "rebuild"
            and self._vm.entries
            and 0 <= self._vm.cursor < len(self._vm.entries)
        ):
            table.move_cursor(row=self._vm.cursor, animate=False)

        status = self.query_one("#tab-status", Static)
        status.update(self._format_status())

    # ------------------------------------------------------------------
    # Cross-region focus — see module docstring for the full graph table
    # ------------------------------------------------------------------

    # Node name → focusable widget id. The dialog node is handled separately because its target
    # depends on ``_active_dialog``.
    _NODE_TO_WIDGET_ID: dict[str, str] = {
        "entry_table": "entries-table",
        "entry_search": "search-input",
        "entry_title": "details-title",
        "entry_content": "details-content",
        "entry_modification_accept": "details-choices",
        "flashcard_table": "linked-flashcards-table",
        "flashcard_search": "linked-flashcards-search-input",
        "relink_choices": "linked-flashcards-relink-choices",
    }

    # Override so external ``tab.focus()`` calls land on the entries table (leftmost in the focus
    # graph) rather than no-op'ing on the non-focusable ``Vertical`` container. Always the entries
    # table regardless of dialog state — caller intent is "enter the tab", not "resume".
    def focus(self, scroll_visible: bool = True) -> "EntryTab":
        try:
            self.query_one("#entries-table", DataTable).focus()
        except Exception:
            pass
        return self

    def _focused_node(self) -> str | None:
        # All four dialogs collapse to one ``"dialog"`` node — their outgoing edges are identical.
        focused = self.screen.focused if self.screen else None
        if focused is None:
            return None
        if self._active_dialog is not None:
            widget_id = dict(self._DIALOG_WIDGETS)[self._active_dialog]
            try:
                if focused is self.query_one(widget_id, Widget):
                    return "dialog"
            except Exception:
                pass
        fid = focused.id
        if fid == "entries-table":
            return "entry_table"
        if fid == "search-input":
            return "entry_search"
        if fid == "details-title":
            return "entry_title"
        if fid == "details-content":
            return "entry_content"
        if fid == "details-choices":
            return "entry_modification_accept"
        if fid == "linked-flashcards-table":
            return "flashcard_table"
        if fid == "linked-flashcards-search-input":
            return "flashcard_search"
        if fid == "linked-flashcards-relink-choices":
            return "relink_choices"
        return None

    def _node_present(self, node: str) -> bool:
        """Whether ``node`` is in the focus graph right now. The multi-select-frozen details panel
        is treated as absent (TextAreas are read-only, choices widget is hidden)."""
        if node == "dialog":
            return self._active_dialog is not None
        state = self._vm.state
        frozen = self._vm.multi_select_active
        if node in ("entry_title", "entry_content"):
            return state is self._vm.State.ENTRIES and not frozen
        if node == "entry_modification_accept":
            return (
                state is self._vm.State.ENTRIES
                and not frozen
                and self._vm.details.is_dirty
            )
        if node in ("flashcard_table", "flashcard_search"):
            return state is self._vm.State.LINKED_FLASHCARDS
        if node == "relink_choices":
            return (
                state is self._vm.State.LINKED_FLASHCARDS
                and self._vm.linked_flashcards.is_relink_dirty
            )
        # entry_table / entry_search are always present.
        return node in ("entry_table", "entry_search")

    def _focus_node(self, node: str) -> bool:
        """Focus ``node``'s widget. Returns False (no focus call) when the node is absent."""
        if not self._node_present(node):
            return False
        if node == "dialog":
            assert self._active_dialog is not None  # implied by _node_present
            widget_id = dict(self._DIALOG_WIDGETS)[self._active_dialog]
            try:
                self.query_one(widget_id, Widget).focus()
                return True
            except Exception:
                return False
        target_id = self._NODE_TO_WIDGET_ID[node]
        try:
            self.query_one(f"#{target_id}").focus()
            return True
        except Exception:
            return False

    # Action wrappers: bubble (via SkipAction) on no-handle so ``Browser`` can run the
    # cross-region fall-through for ``alt+left``/``alt+right``. ``nav_left`` returning the
    # ``"topic_tree"`` sentinel is also a bubble-up — the orchestrator owns the panel-jump.

    def action_nav_left(self) -> None:
        result = self.nav_left()
        if result is False or result == "topic_tree":
            raise SkipAction()

    def action_nav_right(self) -> None:
        if not self.nav_right():
            raise SkipAction()

    def action_nav_up(self) -> None:
        if not self.nav_up():
            raise SkipAction()

    def action_nav_down(self) -> None:
        if not self.nav_down():
            raise SkipAction()

    def nav_up(self) -> bool:
        node = self._focused_node()
        if node is None or not self._node_present(node):
            return False
        if node == "dialog":
            return self._focus_node("entry_table")
        if node == "entry_table":
            return self._focus_node("entry_search")
        if node == "flashcard_table":
            return self._focus_node("flashcard_search")
        if node == "relink_choices":
            return self._focus_node("flashcard_table")
        if node == "entry_modification_accept":
            return self._focus_node("entry_content")
        if node == "entry_content":
            return self._focus_node("entry_title")
        if node == "entry_title":
            return self._focus_node("entry_search")
        return False

    def nav_down(self) -> bool:
        node = self._focused_node()
        if node is None or not self._node_present(node):
            return False
        if node == "entry_search":
            return self._focus_node("entry_table")
        if node == "flashcard_search":
            return self._focus_node("flashcard_table")
        if node == "entry_table":
            return self._focus_node("dialog")
        if node == "flashcard_table":
            # Mirrors entry_content: accept/cancel wins while dirty, else fall through to dialog.
            return self._focus_node("relink_choices") or self._focus_node("dialog")
        if node == "relink_choices":
            return self._focus_node("dialog")
        if node == "entry_title":
            return self._focus_node("entry_content")
        if node == "entry_content":
            return self._focus_node("entry_modification_accept") or self._focus_node("dialog")
        if node == "entry_modification_accept":
            return self._focus_node("dialog")
        return False

    def nav_left(self) -> bool | str:
        """Returns ``"topic_tree"`` sentinel for edges that escape the tab, otherwise a bool."""
        node = self._focused_node()
        if node is None or not self._node_present(node):
            return False
        if node in ("entry_search", "entry_table", "dialog"):
            return "topic_tree"
        if node == "entry_title":
            return self._focus_node("entry_search")
        if node in ("entry_content", "entry_modification_accept"):
            return self._focus_node("entry_table")
        if node == "flashcard_search":
            return self._focus_node("entry_search")
        if node == "flashcard_table":
            return self._focus_node("entry_table")
        if node == "relink_choices":
            return self._focus_node("entry_table")
        return False

    def nav_right(self) -> bool:
        node = self._focused_node()
        if node is None or not self._node_present(node):
            return False
        state = self._vm.state
        if node == "entry_search":
            if state is self._vm.State.ENTRIES:
                return self._focus_node("entry_title")
            if state is self._vm.State.LINKED_FLASHCARDS:
                return self._focus_node("flashcard_table")
        if node == "entry_table":
            if state is self._vm.State.ENTRIES:
                return self._focus_node("entry_content")
            if state is self._vm.State.LINKED_FLASHCARDS:
                return self._focus_node("flashcard_table")
        return False

    # ------------------------------------------------------------------
    # Edit-dialog choice dispatch — called from EditMenu with the chosen label
    # ------------------------------------------------------------------

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
        # Local import — avoid circulating tui.screens through this widget module at import time.
        from rhizome.tui.screens.topic_selector import TopicSelectorScreen

        if self.selection_target_count() == 0:
            return

        def on_dismiss(result: tuple[int, str] | None) -> None:
            if result is None:
                # Cancelled — refocus the edit bar so the user can pick a different action.
                try:
                    self.query_one("#edit-bar", EditMenu).focus()
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
        # Lands the modal's cursor on the cursor entry's current type (single-select only).
        if self.selection_target_count() == 0:
            return

        current: EntryType | None = None
        if not self._vm.multi_select_active and self._vm.entries:
            current = self._vm.entries[self._vm.cursor].entry_type

        def on_dismiss(result: EntryType | None) -> None:
            if result is None:
                try:
                    self.query_one("#edit-bar", EditMenu).focus()
                except Exception:
                    pass
                return
            self.run_worker(
                self._vm.change_type_on_selected_entries(result), exclusive=False,
            )

        self.app.push_screen(TypePickerScreen(current=current), callback=on_dismiss)

    def _dispatch_focus_details_field(self, widget_id: str) -> None:
        # Single-select only — the option list excludes these in multi-select.
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
        # ``set_cursor`` no-ops on equal index, so safe to fire from the programmatic
        # ``move_cursor`` in ``_refresh`` and from the initial seed-to-row-0 on mount.
        if event.data_table.id != "entries-table":
            return
        self._vm.set_cursor(event.cursor_row)

    def _format_status(self) -> str:
        if self._vm.is_loading:
            return "loading…"
        if self._vm.multi_select_active:
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
