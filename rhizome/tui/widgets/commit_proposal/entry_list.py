"""``EntryList`` — focusable ``DataTable`` of pending entries.

Three columns: Title, Type, Topic. The row set is fixed at mount (the proposal can't grow or shrink
its entry list — ``reset()`` re-clones in place), so rows are added once and only their cell
contents are mutated thereafter via ``update_cell_at``. That side-steps the spurious ``Row-
Highlighted`` events ``clear`` + ``add_row`` would otherwise post (Textual posts ``RH(0)`` whenever
the table goes from zero to one rows, which would round-trip back through
``_on_row_highlighted`` and oscillate the VM cursor).

Cursor movement is driven by ``DataTable``'s default ``cursor_up`` / ``cursor_down`` actions; the
resulting ``RowHighlighted`` is forwarded to ``vm.set_cursor``. ``check_action`` disables the
binding at the top/bottom row so the keystroke bubbles to the parent ``CommitProposal``'s
focus-graph bindings.

Cursor-sync feedback loop under fast key-repeat
-----------------------------------------------
The VM's identity guard on ``set_cursor`` handles the simple round-trip — when a single
``move_cursor`` call re-posts ``RowHighlighted`` at the same row, the next ``set_cursor`` no-ops
and the chain dies. But under fast key-repeat on a large table, Textual's ``RowHighlighted``
backlog desynchronizes: by the time we process ``RH(N+1)``, ``cursor_coordinate`` has already
advanced to N+2 with ``RH(N+2)`` queued. If ``_refresh`` then "syncs" the table back to N+1 via
``move_cursor``, two things happen at once: (a) the visible cursor snaps backwards, and (b) a
fresh ``RH(N+1)`` lands in the queue behind ``RH(N+2)``. The two interleave indefinitely — every
step is a genuine cursor change, so the identity guard never gets to fire.

The fix: skip the ``_refresh`` cursor-sync while a ``RowHighlighted`` is being forwarded — in
that path the table is the source of truth and any pushback from us only adds noise. The sync
still runs for VM-initiated moves (boundary navigation from the parent view, ``reset()``'s
cursor clamp), which is where it's actually needed.

Excluded rows render dim + strikethrough across all three columns.

Subscribes to both ``vm.dirty`` and ``vm.details.dirty`` — the latter so cell content re-renders
when an in-place title/content edit is accepted on the focused entry.
"""

from __future__ import annotations

from rich.style import Style
from rich.text import Text
from textual import on
from textual.coordinate import Coordinate
from textual.widgets import DataTable

from rhizome.app.commit_proposal.commit_proposal import CommitProposalModel
from rhizome.tui.keybindings import Keybind
from rhizome.tui.widgets.commit_proposal.messages import SetTopicRequested


class EntryList(DataTable, can_focus=True):

    DEFAULT_CSS = """
    EntryList {
        width: 1fr;
        height: auto;
        min-height: 5;
        max-height: 20;
    }
    """

    BINDINGS = [
        Keybind.ProposalToggleExclude.as_binding("toggle_exclude", show=False),
        Keybind.ProposalCycleType.    as_binding("cycle_type",     show=False),
        Keybind.ProposalSetTopic.     as_binding("set_topic",      show=False),
        # ``up`` / ``down`` use the inherited ``DataTable`` bindings — see module docstring for the
        # cursor-routing rationale. ``e`` is not bound locally either; it bubbles to the parent
        # CommitProposal's "e" binding, which forwards focus into the details panel.
    ]

    _CONTENT_MIN_WIDTH = 15

    def __init__(self, vm: CommitProposalModel, **kwargs) -> None:
        super().__init__(
            show_header=True,
            show_row_labels=False,
            zebra_stripes=True,
            cursor_type="row",
            **kwargs,
        )
        self._vm = vm
        self._content_key = None
        # True for the duration of ``_on_row_highlighted``. ``_refresh`` checks this
        # to skip its cursor-sync ``move_cursor`` — see the module docstring for the fast-scroll
        # feedback loop it guards against.
        self._handling_row_highlighted: bool = False

    def on_mount(self) -> None:
        self.add_columns("Title", "Type", "Topic")
        # Content column starts at the minimum and is widened in ``_fit_content_column`` to absorb
        # whatever horizontal space the auto-sized columns leave behind — keeps long entry text
        # from inducing a horizontal scrollbar.
        self._content_key = self.add_column("Content", width=self._CONTENT_MIN_WIDTH)
        # One-time population. Cell contents are filled in by the initial ``_refresh`` below and
        # by every subsequent ``vm.dirty`` emit, exclusively through ``update_cell_at`` — no
        # ``clear`` / ``add_row`` ever runs again on this table.
        for i in range(len(self._vm.entries)):
            self.add_row("", "", "", "", key=str(i))
        self._vm.subscribe(self._vm.dirty, self._refresh)
        self._vm.details.subscribe(self._vm.details.dirty, self._refresh)
        self._refresh()

    def on_resize(self) -> None:
        self._fit_content_column()

    def _fit_content_column(self) -> None:
        if self._content_key is None or self.size.width <= 0:
            return
        content_col = self.columns.get(self._content_key)
        if content_col is None:
            return
        others = sum(
            c.get_render_width(self) for k, c in self.columns.items() if k != self._content_key
        )
        available = self.size.width - others - 2 * self.cell_padding
        target = max(self._CONTENT_MIN_WIDTH, available)
        if content_col.width != target:
            content_col.width = target
            self.refresh(layout=True)

    def on_unmount(self) -> None:
        self._vm.unsubscribe(self._vm.dirty, self._refresh)
        self._vm.details.unsubscribe(self._vm.details.dirty, self._refresh)

    # ------------------------------------------------------------------
    # Boundary detection — disable cursor_up at the top row and cursor_down at the bottom so the
    # key bubbles up to the parent's own up/down handler.
    # ------------------------------------------------------------------

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "cursor_up":
            return self._vm.cursor is not None and self._vm.cursor > 0
        if action == "cursor_down":
            return (
                self._vm.cursor is not None
                and self._vm.cursor < len(self._vm.entries) - 1
            )
        # Disable DataTable's inherited ``enter → select_cursor`` binding — nothing handles
        # ``RowSelected`` here, and returning False lets enter bubble to the parent's collapse toggle.
        if action == "select_cursor":
            return False
        return True

    # ------------------------------------------------------------------
    # Bindings
    # ------------------------------------------------------------------

    def action_toggle_exclude(self) -> None:
        # State guard: the entry-list stays focusable in DONE so the user can browse the
        # proposal, but every mutator must be off-limits there — the VM asserts EDITING.
        if self._vm.state != CommitProposalModel.State.EDITING:
            return
        self._vm.toggle_exclude_current_entry()

    def action_cycle_type(self) -> None:
        if self._vm.state != CommitProposalModel.State.EDITING:
            return
        self._vm.cycle_current_entry_type()

    def action_set_topic(self) -> None:
        if self._vm.state != CommitProposalModel.State.EDITING:
            return
        self.post_message(SetTopicRequested(scope="current"))

    # ------------------------------------------------------------------
    # DataTable's default cursor actions move the row cursor and post ``RowHighlighted``; this
    # handler pushes the new index into the VM. The VM's equality guard on ``set_cursor`` absorbs
    # the bounce when the move was VM-initiated.
    # ------------------------------------------------------------------

    @on(DataTable.RowHighlighted)
    def _on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # The flag scopes for the entire ``vm.set_cursor`` call — both the ``vm.details.dirty``
        # subscriber (fired from ``_sync_details``) and the ``vm.dirty`` subscriber run
        # synchronously inside the emit chain, so both ``_refresh`` invocations see the flag.
        self._handling_row_highlighted = True
        try:
            self._vm.set_cursor(event.cursor_row)
        finally:
            self._handling_row_highlighted = False

    # ------------------------------------------------------------------
    # Per-row cursor tint — DataTable's component-class lookup is flat per cell, so we hook
    # ``_get_styles_to_render_cell`` (bypassing its lru_cache, which doesn't key on row) and
    # paint a different bg when the cursor lands on an excluded entry.
    # ------------------------------------------------------------------

    # A muted/dimmer take on the default focused-cursor blue (Textual's ``$block-cursor-background``
    # is ~``rgb(30,108,180)`` under the dark theme); we darken + desaturate so excluded rows still
    # read as "cursor is here" but visibly muted vs. an included row.
    _EXCLUDED_CURSOR_STYLE = Style(bgcolor="rgb(28,52,86)")

    def _get_styles_to_render_cell(
        self,
        is_header_cell: bool,
        is_row_label_cell: bool,
        is_fixed_style_cell: bool,
        hover: bool,
        cursor: bool,
        show_cursor: bool,
        show_hover_cursor: bool,
        has_css_foreground_priority: bool,
        has_css_background_priority: bool,
    ) -> tuple[Style, Style]:
        component_style, post_style = super()._get_styles_to_render_cell(
            is_header_cell, is_row_label_cell, is_fixed_style_cell,
            hover, cursor, show_cursor, show_hover_cursor,
            has_css_foreground_priority, has_css_background_priority,
        )
        if cursor and show_cursor and not is_header_cell and not is_row_label_cell:
            row = self.cursor_row
            if 0 <= row < len(self._vm.entries) and self._vm.is_excluded(row):
                component_style += self._EXCLUDED_CURSOR_STYLE
                if has_css_background_priority:
                    post_style += self._EXCLUDED_CURSOR_STYLE
        return component_style, post_style

    # Monkey-patch alert (sorry, future dev). The base ``_get_styles_to_render_cell`` is
    # ``@functools.lru_cache``-decorated, and ``DataTable._clear_caches`` reaches in and calls
    # ``self._get_styles_to_render_cell.cache_clear()`` directly. Our override deliberately
    # isn't cached — the cache key doesn't include row_index, so caching would prevent the
    # per-row tint from updating — but we still have to satisfy that attribute lookup or
    # mount blows up with ``AttributeError: 'function' object has no attribute 'cache_clear'``.
    # If a future Textual release drops the lru_cache or stops calling ``cache_clear`` on it,
    # this stub becomes dead and can go.
    _get_styles_to_render_cell.cache_clear = lambda: None  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # VM → View
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        cursor = self._vm.cursor
        for i, entry in enumerate(self._vm.entries):
            is_excluded = self._vm.is_excluded(i)
            style = "dim strike" if is_excluded else ""

            title = Text(entry.title or "(untitled)", style=style)
            entry_type = Text(entry.entry_type.value, style=style or "dim")
            topic = Text(entry.topic_name or "(none)", style=style or "dim")
            # Collapse newlines so multi-line content stays on one row; the cell itself will
            # truncate per the column's width budget.
            content_preview = " ".join((entry.content or "").split()) or "(empty)"
            content = Text(content_preview, style=style or "dim")

            self.update_cell_at(Coordinate(i, 0), title)
            self.update_cell_at(Coordinate(i, 1), entry_type)
            self.update_cell_at(Coordinate(i, 2), topic)
            self.update_cell_at(Coordinate(i, 3), content)

        # Sync the table's visual cursor to vm.cursor for VM-initiated moves (boundary navigation
        # from the parent view, ``reset()``'s clamp). Suppressed inside ``RowHighlighted`` handling
        # because the table is the source of truth there and pushing back would re-post the event —
        # under fast key-repeat that closes the feedback loop documented in the module docstring.
        if (
            not self._handling_row_highlighted
            and cursor is not None
            and self.cursor_row != cursor
        ):
            self.move_cursor(row=cursor, animate=False)

        # Auto-width columns may shift as cell content changes, so re-fit the content column.
        self._fit_content_column()
