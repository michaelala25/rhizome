
from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.widgets import Static
from textual.widget import Widget

from rhizome.app.options import OptionScope
from rhizome.app.options_editor import OptionsEditorVM
from rhizome.tui.widgets.navigable_feed_item_view_base import NavigableFeedItemViewBase
from rhizome.tui.widgets.options_editor.actions import OptionsEditorActions
from rhizome.tui.widgets.options_editor.list_container import OptionsListContainer


class OptionsEditor(NavigableFeedItemViewBase[OptionsEditorVM]):

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
        Binding("ctrl+a", "apply"),
        Binding("ctrl+r", "reset"),
        Binding("ctrl+c", "dismiss"),

        # Focus graph navigation
        Binding("alt+up", "focus_neighbour('up')"),
        Binding("alt+down", "focus_neighbour('down')"),

        # Fall-through for key-events not processed by children - indicates cursor navigation at boundaries 
        # (up from first action item, down from last option row) - translates to a focus_neighbour + set cursor.
        Binding("up", "navigate_cursor('up')", show=False),
        Binding("down", "navigate_cursor('down')", show=False),
    ]

    def __init__(self, vm: OptionsEditorVM, **kwargs: Any) -> None:
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
        self._focus_first()

    def on_unmount(self) -> None:
        self.vm.detach()
        super().on_unmount()

    def _focus_first(self) -> None:
        if self.options_list:
            self.options_list.focus()

    def on_focus(self, event) -> None:
        super().on_focus(event)
        self._focus_first()

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
        focused = self.screen.focused if self.screen is not None else None
        source = self._owning_focus_node_id(focused)
        target = self._neighbour_id(source, direction)
        if target is not None:
            self._focus_node(target)

    def action_navigate_cursor(self, direction: str) -> None:
        focused = self.screen.focused if self.screen is not None else None
        source = self._owning_focus_node_id(focused)
        target = self._neighbour_id(source, direction)
        
        if target == "oe-list":
            assert self.options_list
            if not self.options_list._option_rows:
                return
            self.options_list._cursor = len(self.options_list._option_rows) - 1
            self.options_list.focus()
        elif target == "oe-actions":
            assert self.actions_list
            self.actions_list._cursor = 0
            self.actions_list.focus()


    def _owning_focus_node_id(self, widget: Widget | None) -> str | None:
        # Walk up from the focused widget until we find the scroll area or the choices footer.
        node: Widget | None = widget
        while node is not None and node is not self:
            if node.id in ("oe-list", "oe-actions"):
                return node.id
            # An OptionSpecView is a descendant of the scroll area, but the loop's parent walk
            # will hit oe-scroll naturally on the next iteration.
            node = node.parent
        return None
    
    def _neighbour_id(self, source: str | None, direction: str) -> str | None:
        if source == "oe-list" and direction == "down":
            return "oe-actions"
        if source == "oe-actions" and direction == "up":
            return "oe-list"
        return None

    def _focus_node(self, node_id: str, *, direction: str | None = None) -> None:
        if node_id == "oe-list":
            if self.options_list:
                self.options_list.focus()
        if node_id == "oe-actions":
            if self.actions_list:
                self.actions_list.focus()    