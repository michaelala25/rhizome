"""Knowledge-entry tab view. DataTable + right pane (details ↔ linked-flashcards) + bottom dialog
slot. Owns the dialog mutex, the global tab keys, and the cross-region focus graph.

Dialog mutex: a single ``_active_dialog`` slot toggled via ``show_dialog`` / ``hide_dialog`` /
``toggle_dialog``. Mutators flip the ``-visible`` class on the chosen widget, call its
``prepare_for_show()`` hook, and run focus rescue (focus the new dialog, or fall back to the
entries table on hide). State transitions auto-dismiss any open dialog.

Alt-arrow navigation: the tab uses ``FocusOrchestrationMixin`` to walk its focus graph (see
``FOCUS_GRAPH``). When a step has no in-graph target, ``action_focus_neighbour`` raises
``SkipAction`` so the key bubbles to ``Browser`` for the cross-region hop back to the topic
panel — that's how alt+left from search/table/dialog escapes the tab.

The graph uses widget ids 1:1, with one pseudo-id ``"dialog"`` for the dialog mutex slot —
``_resolve_node`` maps it to the currently-mounted dialog widget, ``_current_focus_node``
maps a focused dialog widget back to it, and ``_is_node_available("dialog")`` gates on
``_active_dialog is not None``. State-dependent nodes (details-* in ENTRIES,
linked-flashcards-* in LINKED_FLASHCARDS, plus the multi-select-frozen exclusion of the
details panel) are gated in ``_is_node_available`` as well, so the fallback lists on the right
edges of search/entries-table resolve to whichever pane is live at the time.
"""

from __future__ import annotations

from typing import Any, Literal

from rich.text import Text
from textual import on
from textual.actions import SkipAction
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.widget import Widget
from textual.widgets import DataTable, Input, Rule, Static, TextArea

from rhizome.app.browser.tabs.entries.tab import EntryTabModel
from rhizome.db.models import EntryType
from rhizome.tui.keybindings import Keybind, binding_hint
from rhizome.tui.widgets.shared.search_bar import SearchBar
from rhizome.tui.widgets.browser.tabs.entries.delete import EntriesDeleteMenu
from rhizome.tui.widgets.browser.tabs.entries.details import EntryDetails
from rhizome.tui.widgets.browser.tabs.entries.edit import EditMenu
from rhizome.tui.widgets.browser.tabs.entries.entry_preview import EntryPreview
from rhizome.tui.widgets.browser.tabs.entries.entry_table import EntryTable
from rhizome.tui.widgets.browser.tabs.entries.filter import FilterMenu
from rhizome.tui.widgets.browser.tabs.entries.linked_flashcards_panel import LinkedFlashcardsPanel
from rhizome.tui.widgets.browser.tabs.entries.sort import EntriesSortMenu
from rhizome.tui.widgets.browser.tabs.entries.type_picker import TypePickerScreen
from rhizome.tui.widgets.shared.focus_orchestration import FocusGraph, FocusOrchestrationMixin

_DialogName = Literal["delete", "sort", "filter", "edit"]


class EntryTab(Vertical, FocusOrchestrationMixin):
    """Tab container. See module docstring for the dialog mutex and focus-graph contracts."""

    # Vertical's own ``can_focus = False`` (in its ``__dict__``) wins MRO over the mixin's True,
    # so we restore it explicitly here — required for the mixin's ``on_focus`` delegation to fire
    # when external callers focus the tab.
    can_focus = True

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
    /* Right pane is lazy-mounted (see ``_swap_right_pane``) — only the active pane lives in the
       DOM at any time, which keeps the StyleSheet apply scope small on state flips. ``-state-*``
       still drives the table-column width / table height / preview visibility on the left side. */
    EntryTab.-state-entries #table-column {
        width: 60%;
    }
    EntryTab.-state-linked-flashcards #table-column {
        width: 50%;
    }
    EntryTab EntryDetails,
    EntryTab LinkedFlashcardsPanel {
        width: 1fr;
        height: 1fr;
    }
    /* Status row sits in #table-column so it aligns with the linked-flashcards docked status. */
    EntryTab #tab-status {
        height: 1;
        color: $foreground-muted;
        text-style: dim;
        padding: 0 1;
    }
    /* Dialogs are lazy-mounted — at most one lives in the DOM, only while open. No default
       ``display: none`` / ``.-visible`` toggle: presence in the DOM is what makes them visible. */
    EntryTab #delete-confirm {
        height: 4;
        margin: 1 0 0 0;
        padding: 0 1;
        border-top: solid #3a3a3a;
        color: rgb(200,200,200);
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
        Keybind.BrowserDelete.     as_binding("tab_toggle_dialog('delete')", "Delete",          show=True),
        Keybind.BrowserSort.       as_binding("tab_toggle_dialog('sort')",   "Sort",            show=True),
        Keybind.BrowserFilter.     as_binding("tab_toggle_dialog('filter')", "Filter",          show=True),
        Keybind.BrowserEdit.       as_binding("tab_toggle_dialog('edit')",   "Edit",            show=True),
        Keybind.BrowserRelink.     as_binding("tab_toggle_relink",           "Link flashcards", show=True),
        Keybind.BrowserMultiSelect.as_binding("tab_toggle_multi_select",     "Multi-select",    show=True),
        Keybind.BrowserCycleMode.  as_binding("tab_cycle_mode",              "Cycle mode",      show=True),
        Keybind.InnerFocusLeft .as_binding("focus_neighbour('left')",  show=False),
        Keybind.InnerFocusRight.as_binding("focus_neighbour('right')", show=False),
        Keybind.InnerFocusUp   .as_binding("focus_neighbour('up')",    show=False),
        Keybind.InnerFocusDown .as_binding("focus_neighbour('down')",  show=False),
    ]

    # Static focus graph. The ``"dialog"`` node is a pseudo-id that resolves to whichever dialog
    # is currently mounted (see ``_resolve_node``); ``_current_focus_node`` maps a focused
    # dialog widget back to it. State-dependent right-pane nodes (``details-*`` in ENTRIES,
    # ``linked-flashcards-*`` in LINKED_FLASHCARDS) are gated in ``_is_node_available``, so the
    # fallback lists on the right edges of search/entries-table resolve to whichever pane is live.
    # Nodes with no ``left`` edge (search-input, entries-table, dialog) bubble via SkipAction so
    # ``Browser`` runs the cross-region hop back to the topic panel.
    FOCUS_GRAPH = FocusGraph(
        source="entries-table",
        edges={
            "search-input": {
                "down":  "entries-table",
                "right": ["details-title", "linked-flashcards-table"],
            },
            "entries-table": {
                "up":    "search-input",
                "down":  "dialog",
                "right": ["details-content", "linked-flashcards-table"],
            },
            "dialog": {
                "up": "entries-table",
            },
            # ENTRIES right pane
            "details-title": {
                "up":   "search-input",
                "down": "details-content",
                "left": "search-input",
            },
            "details-content": {
                "up":   "details-title",
                "down": ["details-choices", "dialog"],
                "left": "entries-table",
            },
            "details-choices": {
                "up":   "details-content",
                "down": "dialog",
                "left": "entries-table",
            },
            # LINKED_FLASHCARDS right pane
            "linked-flashcards-search-input": {
                "down": "linked-flashcards-table",
                "left": "search-input",
            },
            "linked-flashcards-table": {
                "up":   "linked-flashcards-search-input",
                "down": ["linked-flashcards-relink-choices", "dialog"],
                "left": "entries-table",
            },
            "linked-flashcards-relink-choices": {
                "up":   "linked-flashcards-table",
                "down": "dialog",
                "left": "entries-table",
            },
        },
    )

    def __init__(
        self,
        view_model: EntryTabModel,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = view_model
        # Dialog mutex — at most one visible. Mutators run through ``toggle_dialog`` /
        # ``hide_dialog`` so visibility, focus, and ``prepare_for_show`` stay coordinated.
        self._active_dialog: _DialogName | None = None
        # VM state at last refresh — used to auto-dismiss the dialog on transitions.
        self._last_state: EntryTabModel.State | None = None
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
                yield SearchBar[EntryTabModel](
                    self._vm, id="search-input",
                )
                yield table
                yield EntryPreview(self._vm, id="entry-content-preview")
                yield Static("", id="tab-status")

            yield Rule(orientation="vertical", line_style="solid", id="tab-body-rule")
            # Only the active pane is in the DOM at any time. ``_swap_right_pane`` (driven from
            # ``_refresh`` on state change) unmounts the outgoing widget and mounts the incoming
            # one; each pane re-subscribes its ``vm.dirty`` from ``on_mount`` so the subscription
            # lifetime tracks the mount lifetime.
            yield self._make_right_pane(self._vm.state)
        # Dialogs are lazy-mounted between ``#tab-body`` and ``#tab-keybindings-rule`` via
        # ``_mount_dialog_widget``; at most one lives in the DOM at a time. See the orchestration
        # block below.
        yield Rule(line_style="solid", id="tab-keybindings-rule")
        yield Static(self._keybindings_text(), id="tab-keybindings")

    def on_mount(self) -> None:
        self._vm.subscribe(self._vm.Callbacks.OnDirty, self._refresh)
        # Paint immediately if the VM was bootstrapped before mount.
        self._refresh()

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.Callbacks.OnDirty, self._refresh)

    # ------------------------------------------------------------------
    # Bottom keybindings line
    # ------------------------------------------------------------------

    def _keybindings_text(self) -> str:
        # Per-dialog keys (← / → / enter / esc inside an open dialog) are documented by the dialog
        # widgets themselves. Command rows come from BINDINGS; the combined focus-nav row is appended.
        rows = [
            (b.key_display or b.key.split(",")[0], b.description.lower())
            for b in self.BINDINGS if b.show and b.description
        ]
        rows.append(("alt+←↑→↓", "navigate"))
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
    # Dialog orchestration — lazy mount, mutex of one
    # ------------------------------------------------------------------
    #
    # At most one dialog is mounted at a time, lifted in/out of the slot between ``#tab-body`` and
    # ``#tab-keybindings-rule`` as the user opens / closes / swaps them. Presence in the DOM is what
    # makes the dialog visible — no ``.-visible`` class needed.
    #
    # ``prepare_for_show`` + ``focus`` run via ``call_after_refresh`` because ``mount`` only queues
    # the insertion; the widget hasn't composed yet when ``mount`` returns. Deferring until the next
    # refresh tick guarantees children (if the dialog has any) are queryable by the time setup runs.

    def toggle_dialog(self, name: _DialogName) -> None:
        if self._active_dialog == name:
            self.hide_dialog()
        else:
            self.show_dialog(name)

    def show_dialog(self, name: _DialogName) -> None:
        if self._active_dialog == name:
            return
        self._unmount_dialog_widget(self._active_dialog)
        self._active_dialog = name
        self._mount_dialog_widget(name)

    def hide_dialog(self) -> None:
        if self._active_dialog is None:
            return
        name = self._active_dialog
        self._active_dialog = None
        # Park focus on the entries table before unmount so it isn't orphaned when the dialog (which
        # likely held focus) is removed.
        try:
            self.query_one("#entries-table", DataTable).focus()
        except Exception:
            pass
        self._unmount_dialog_widget(name)

    def _make_dialog(self, name: _DialogName) -> Widget:
        if name == "delete":
            return EntriesDeleteMenu(self._vm, self, id="delete-confirm")
        if name == "sort":
            return EntriesSortMenu(self._vm, on_close=self.hide_dialog, id="sort-bar")
        if name == "filter":
            return FilterMenu(self._vm, self, id="filter-dialog")
        if name == "edit":
            return EditMenu(self._vm, self, id="edit-bar")
        raise ValueError(f"unknown dialog: {name!r}")

    def _mount_dialog_widget(self, name: _DialogName) -> None:
        dialog = self._make_dialog(name)
        try:
            anchor = self.query_one("#tab-keybindings-rule")
        except Exception:
            return
        self.mount(dialog, before=anchor)
        self.call_after_refresh(self._post_mount_dialog_setup)

    def _unmount_dialog_widget(self, name: _DialogName | None) -> None:
        if name is None:
            return
        widget_id = dict(self._DIALOG_WIDGETS)[name]
        try:
            self.query_one(widget_id, Widget).remove()
        except Exception:
            pass

    def _post_mount_dialog_setup(self) -> None:
        # ``self._active_dialog`` is the source of truth — if the user toggled again before this
        # callback fired, the currently-mounted dialog may differ from what was passed to mount.
        name = self._active_dialog
        if name is None:
            return
        widget_id = dict(self._DIALOG_WIDGETS)[name]
        try:
            widget = self.query_one(widget_id, Widget)
        except Exception:
            return
        prepare = getattr(widget, "prepare_for_show", None)
        if prepare is not None:
            prepare()
        widget.focus()

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

        # State transitions auto-dismiss any open dialog (its targets/axes/options aren't generally
        # meaningful across a layout switch) and swap the right-pane widget (lazy-mount: only the
        # pane for the current state lives in the DOM).
        if self._last_state is not None and state != self._last_state:
            if self._active_dialog is not None:
                self.hide_dialog()
            self._swap_right_pane(state)
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
    # Right-pane lazy mount — only the active pane lives in the DOM
    # ------------------------------------------------------------------
    #
    # The right pane (``EntryDetails`` in ``ENTRIES``, ``LinkedFlashcardsPanel`` in
    # ``LINKED_FLASHCARDS``) is mounted on demand rather than mounted-and-hidden, so state flips
    # don't drag the inactive subtree through every ``StyleSheet.apply``. Each pane subscribes its
    # own ``vm.dirty`` in ``on_mount`` and unsubscribes in ``on_unmount`` — re-subscribe cost on
    # mode switch is O(1) per pane, dwarfed by the avoided CSS work.

    def _make_right_pane(self, state: EntryTabModel.State) -> Widget:
        """Construct the right-pane widget for ``state``. Pure factory — the caller is responsible
        for yielding it from ``compose`` or mounting it in ``_swap_right_pane``."""
        if state is self._vm.State.ENTRIES:
            return EntryDetails(self._vm.details)
        return LinkedFlashcardsPanel(self._vm.linked_flashcards)

    def _current_right_pane(self) -> Widget | None:
        """Whichever right-pane widget is currently mounted (``EntryDetails`` or
        ``LinkedFlashcardsPanel``), or ``None`` if neither is mounted yet."""
        for cls in (EntryDetails, LinkedFlashcardsPanel):
            try:
                return self.query_one(cls)
            except Exception:
                continue
        return None

    def _swap_right_pane(self, new_state: EntryTabModel.State) -> None:
        """Unmount the outgoing pane and mount the one for ``new_state``. Rescues focus to the
        entries table first if it'd otherwise be orphaned inside the outgoing pane."""
        try:
            body = self.query_one("#tab-body", Horizontal)
        except Exception:
            return

        current = self._current_right_pane()
        if current is not None:
            focused = self.screen.focused if self.screen else None
            focus_inside = focused is not None and (
                focused is current or current in focused.ancestors_with_self
            )
            if focus_inside:
                try:
                    self.query_one("#entries-table", DataTable).focus()
                except Exception:
                    pass
            current.remove()

        body.mount(self._make_right_pane(new_state))

    # ------------------------------------------------------------------
    # Cross-region focus — mixin walks the graph above; we just contribute the dialog-pseudo-node
    # plumbing and the state-dependent availability gating.
    # ------------------------------------------------------------------

    def action_focus_neighbour(self, direction: str) -> None:
        # Raise SkipAction on no-handle so the keystroke bubbles to ``Browser`` for cross-region
        # handling (e.g., alt+left from search/table/dialog hops to the topic panel).
        if self.focus_neighbour(direction) is None:  # type: ignore[arg-type]
            raise SkipAction()

    def _resolve_node(self, node_id: str) -> Widget | None:
        # The ``"dialog"`` pseudo-node resolves to whichever of the four dialog widgets is
        # currently mounted — the dialog mutex guarantees at most one is live at a time.
        if node_id == "dialog":
            if self._active_dialog is None:
                return None
            widget_id = dict(self._DIALOG_WIDGETS)[self._active_dialog]
            try:
                return self.query_one(widget_id, Widget)
            except Exception:
                return None
        return super()._resolve_node(node_id)

    def _current_focus_node(self) -> str | None:
        # Map a focused dialog widget back to the ``"dialog"`` graph node. Matches the original
        # equality-only behavior: focus on a dialog child (e.g. the filter dialog's inputs) is
        # NOT treated as "in dialog" — it falls through and the keystroke bubbles, same as before.
        if self._active_dialog is not None:
            focused = self.screen.focused if self.screen else None
            if focused is not None:
                widget_id = dict(self._DIALOG_WIDGETS)[self._active_dialog]
                try:
                    if focused is self.query_one(widget_id, Widget):
                        return "dialog"
                except Exception:
                    pass
        return super()._current_focus_node()

    def _is_node_available(self, node_id: str) -> bool:
        # Multi-select-frozen details panel is treated as absent (TextAreas are read-only, choices
        # widget is hidden). Otherwise gating follows the layout: details-* live in ENTRIES,
        # linked-flashcards-* live in LINKED_FLASHCARDS, and the bottom-most edit nodes
        # (-choices / relink-choices) additionally require a dirty buffer.
        if node_id == "dialog":
            return self._active_dialog is not None
        state = self._vm.state
        frozen = self._vm.multi_select_active
        if node_id in ("details-title", "details-content"):
            return state is self._vm.State.ENTRIES and not frozen
        if node_id == "details-choices":
            return (
                state is self._vm.State.ENTRIES
                and not frozen
                and self._vm.details.is_dirty
            )
        if node_id in ("linked-flashcards-table", "linked-flashcards-search-input"):
            return state is self._vm.State.LINKED_FLASHCARDS
        if node_id == "linked-flashcards-relink-choices":
            return (
                state is self._vm.State.LINKED_FLASHCARDS
                and self._vm.linked_flashcards.is_relink_dirty
            )
        # entries-table / search-input — always available
        return True

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

    @on(DataTable.RowHighlighted)
    def _on_entries_row_highlighted(
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
