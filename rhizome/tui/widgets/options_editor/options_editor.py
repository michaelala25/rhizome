
from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.message import Message
from textual.widgets import Static

from rhizome.app.options import OptionScope
from rhizome.app.options_editor import OptionsEditorModel
from rhizome.tui.widgets.shared.navigable_feed_item import NavigableFeedItemViewBase
from rhizome.tui.widgets.options_editor.actions import OptionsEditorActions
from rhizome.tui.widgets.options_editor.list_container import OptionsListContainer
from rhizome.tui.widgets.shared.focus_orchestration import FocusGraph, FocusOrchestrationMixin
from rhizome.tui.keybindings import Keybind


class OptionsEditor(NavigableFeedItemViewBase[OptionsEditorModel], FocusOrchestrationMixin):

    class Dismissed(Message):
        """Public-surface dismiss request — the chat pane catches this and drops the feed
        entry by calling ``vm._remove_feed``. The footer's ``OptionsEditorActions.Dismissed``
        and the editor's own ``ctrl+c`` binding both funnel through ``action_dismiss``."""

        def __init__(self, editor: "OptionsEditor") -> None:
            super().__init__()
            self.editor = editor

        @property
        def control(self) -> "OptionsEditor":
            return self.editor

    DEFAULT_CSS = """
    OptionsEditor {
        layout: vertical;
        background: transparent;
        height: auto;
        padding: 0;
    }
    OptionsEditor #oe-header {
        height: auto;
        padding: 0 1;
        background: transparent;
    }
    OptionsEditor #oe-actions {
        margin: 1 0;
    }
    OptionsEditor #oe-hints {
        height: 1;
        padding: 0 1;
        background: transparent;
    }
    """

    BINDINGS = [
        # Lifecycle
        Keybind.OptionsApply.  as_binding("apply",   "Apply",   show=True),
        Keybind.OptionsReset.  as_binding("reset",   "Reset",   show=True),
        Keybind.OptionsDismiss.as_binding("dismiss", "Dismiss", show=True),

        # Focus graph navigation
        Keybind.FocusUp.  as_binding("focus_neighbour('up')",   show=False),
        Keybind.FocusDown.as_binding("focus_neighbour('down')", show=False),

        # Fall-through for key-events not processed by children - indicates cursor navigation at boundaries
        # (up from first action item, down from last option row) - translates to a focus_neighbour + set cursor.
        Keybind.CursorUp.  as_binding("navigate_cursor('up')",   show=False),
        Keybind.CursorDown.as_binding("navigate_cursor('down')", show=False),
    ]

    FOCUS_GRAPH = FocusGraph(
        source="oe-list",
        edges={
            "oe-list":    {"down": "oe-actions"},
            "oe-actions": {"up":   "oe-list"},
        },
    )

    def __init__(self, vm: OptionsEditorModel, **kwargs: Any) -> None:
        super().__init__(vm, **kwargs)

    @property
    def vm(self):
        return self._vm

    # ------------------------------------------------------------------
    # Layout 
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(self._header_text(), id="oe-header")

        yield OptionsListContainer(self.vm, id="oe-list")
        yield OptionsEditorActions(self.vm, id="oe-actions")

        yield Static(self._hint_text(), id="oe-hints")

    def _header_text(self) -> Text:
        text = Text()
        # Title
        text.append("Options Editor\n", style="bold rgb(255,80,80)")
        # Description
        scope = "root" if self._vm.scope == OptionScope.Root else "session"
        text.append(f"Editing {scope}-scope options.", style="rgb(112,112,112)")
        return text

    def _hint_text(self) -> str:
        rows = [
            ("↑↓", "navigate"),
            ("alt+↑↓", "jump to choices"),
        ]
        return "   ".join(f"[#a0a0a0]{k}[/] [#707070]{label}[/]" for k, label in rows)
    
    @property
    def options_list(self):
        try:
            return self.query_one("#oe-list", OptionsListContainer)
        except:
            return None

    @property
    def actions_list(self):
        try:
            return self.query_one("#oe-actions", OptionsEditorActions)
        except:
            return None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        self.focus_first()

    def on_unmount(self) -> None:
        self.vm.detach()
        super().on_unmount()

    def _refresh(self) -> None:
        # Subwidgets subscribe to vm.dirty independently, parent has no rendered surface.
        pass

    # ------------------------------------------------------------------
    # Lifecycle actions
    # ------------------------------------------------------------------

    async def action_apply(self) -> None:
        await self.vm.apply()

    def action_reset(self) -> None:
        self.vm.reset()

    def action_dismiss(self) -> None:
        # Chat pane catches ``OptionsEditor.Dismissed`` and drops the feed entry — the only
        # authoritative removal path. The footer's ``OptionsEditorActions.Dismissed`` is
        # caught below and funnels through this same action.
        self.post_message(self.Dismissed(self))

    @on(OptionsEditorActions.Dismissed)
    def _on_actions_dismissed(self, event: OptionsEditorActions.Dismissed) -> None:
        event.stop()
        self.action_dismiss()


    # ------------------------------------------------------------------
    # Focus orchestration
    # ------------------------------------------------------------------

    def action_focus_neighbour(self, direction: str) -> None:
        """``alt+arrow`` hard refocus across the two-node graph. Leaves cursors untouched."""
        self.focus_neighbour(direction)  # type: ignore[arg-type]

    def action_navigate_cursor(self, direction: str) -> None:
        """Plain up/down at a boundary — move focus across nodes AND seed the target cursor so
        the entry row matches the direction of travel (arriving from below lands at the last row;
        arriving from above lands at the first choice)."""
        target_id = self.focus_neighbour(direction)  # type: ignore[arg-type]
        if target_id is None:
            return

        if target_id == "oe-list":
            opts = self.options_list
            if opts and opts._option_rows:
                opts._cursor = len(opts._option_rows) - 1
                if opts.current_option_row:
                    opts.current_option_row.focus()
        elif target_id == "oe-actions":
            acts = self.actions_list
            if acts:
                acts._cursor = 0
                acts._refresh()
