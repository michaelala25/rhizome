"""Main screen — tabbed chat sessions, log panes, and StatusBar."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import TabbedContent, TabPane

from rhizome.app.options import Options
from rhizome.tui.keybindings import Keybind
from rhizome.tui.types import DatabaseCommitted, UserFeedback
from rhizome.tui.widgets.chat_area.chat_area import ChatArea
from rhizome.tui.widgets.logging_pane import LoggingPane
from rhizome.tui.widgets.workspace.workspace import Workspace


class LogTabPane(TabPane):
    """A TabPane that composes a LoggingPane for viewing log output."""

    LOG_TAB_ID = "logs-tab"

    def __init__(self, *, tab_max_length: int = 20, **kwargs) -> None:
        self.full_name: str = "Logs"
        self._tab_max_length: int = tab_max_length
        super().__init__(self._truncated_label(), id=self.LOG_TAB_ID, **kwargs)

    def _truncated_label(self) -> str:
        if len(self.full_name) > self._tab_max_length:
            return self.full_name[: self._tab_max_length] + "\u2026"
        return self.full_name

    def update_tab_max_length(self, new_length: int) -> None:
        self._tab_max_length = new_length
        tabbed_content = self.screen.query_one("#tabs", TabbedContent)
        tab_widget = tabbed_content.get_tab(self.id)
        tab_widget.label = self._truncated_label()

    @on(UserFeedback)
    def _on_user_feedback(self, event: UserFeedback) -> None:
        self.notify(event.text, severity=event.severity)

    def compose(self) -> ComposeResult:
        yield LoggingPane()


class ChatTabPane(TabPane):
    """A TabPane that composes a Workspace (the conversation stack) as its content.

    Stores the full (untruncated) tab name and reactively re-truncates
    the displayed label when ``tab_name_len`` changes.
    """

    def __init__(self, title: str, *, services, tab_max_length: int = 20, **kwargs) -> None:
        self.full_name: str = title
        self._services = services
        self._tab_max_length: int = tab_max_length
        super().__init__(self._truncated_label(), **kwargs)

    @property
    def workspace(self) -> Workspace:
        """Return the mounted workspace."""
        return self.query_one(Workspace)

    def _truncated_label(self) -> str:
        """Return ``full_name`` truncated to ``_tab_max_length`` characters."""
        if len(self.full_name) > self._tab_max_length:
            return self.full_name[: self._tab_max_length] + "\u2026"
        return self.full_name

    def update_tab_max_length(self, new_length: int) -> None:
        """Update the max length and re-truncate the displayed tab label."""
        self._tab_max_length = new_length
        self._update_tab_label()

    def _update_tab_label(self) -> None:
        """Re-truncate ``full_name`` and apply to the Tab widget."""
        tabbed_content = self.screen.query_one("#tabs", TabbedContent)
        tab_widget = tabbed_content.get_tab(self.id)
        tab_widget.label = self._truncated_label()

    def notify_database_committed(self, event: DatabaseCommitted) -> None:
        """No-op for now: the workspace's resource panels don't yet refresh on DB commit. TODO: wire to
        ``self.workspace.model.resource_loader`` once it grows a refresh entry point, so entries the agent
        writes mid-conversation surface without reopening the loader."""

    @on(UserFeedback)
    def _on_user_feedback(self, event: UserFeedback) -> None:
        self.notify(event.text, severity=event.severity)

    def compose(self) -> ComposeResult:
        yield Workspace(services=self._services)


class MainScreen(Screen):
    """Primary screen: composes tabbed panes and a StatusBar."""

    BINDINGS = [
        Keybind.NewTab.   as_binding("new_tab",    "New tab",      show=True),
        Keybind.CloseTab. as_binding("close_tab",  "Close tab",    show=True, priority=True),
        Keybind.NextTab.  as_binding("next_tab",   "Next tab",     show=True, priority=True),
        Keybind.PrevTab.  as_binding("prev_tab",   "Previous tab", show=True, priority=True),
        Keybind.FocusChat.as_binding("focus_chat", "Focus chat",   show=True, priority=True),
    ]

    DEFAULT_CSS = """
    MainScreen {
        layout: grid;
        grid-size: 1;
        grid-rows: 1fr;
    }
    TabbedContent {
        height: 1fr;
    }
    TabPane {
        height: 1fr;
        padding: 0;
    }
    Workspace {
        height: 1fr;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._tab_counter: int = 1

    def notify_database_committed(self, event: DatabaseCommitted) -> None:
        """Propagate to all ChatTabPanes (LogTabPanes don't need DB notifications)."""
        for pane in self.query(ChatTabPane):
            pane.notify_database_committed(event)

    def compose(self) -> ComposeResult:
        max_len = self.app.options.get(Options.TabMaxLength)  # type: ignore[attr-defined]
        with TabbedContent(id="tabs"):
            yield ChatTabPane(
                "Session 1",
                services=self.app.services,  # type: ignore[attr-defined]
                tab_max_length=max_len,
                id="session-1",
            )

    async def _add_log_tab(self) -> None:
        """Open the logs tab, or switch to it if it already exists."""
        tabs = self.query_one("#tabs", TabbedContent)
        existing = tabs.query(f"#{LogTabPane.LOG_TAB_ID}")
        if existing:
            tabs.active = LogTabPane.LOG_TAB_ID
            existing.first().query_one("#log-output").focus()
            return
        max_len = self.app.options.get(Options.TabMaxLength)  # type: ignore[attr-defined]
        pane = LogTabPane(tab_max_length=max_len)
        await tabs.add_pane(pane)
        tabs.active = LogTabPane.LOG_TAB_ID
        pane.query_one("#log-output").focus()

    async def _add_tab(self, label: str | None = None) -> None:
        """Create a new chat session tab."""
        self._tab_counter += 1
        tab_id = f"session-{self._tab_counter}"
        tab_label = label or f"Session {self._tab_counter}"
        tabs = self.query_one("#tabs", TabbedContent)
        max_len = self.app.options.get(Options.TabMaxLength)  # type: ignore[attr-defined]
        pane = ChatTabPane(
            tab_label,
            services=self.app.services,  # type: ignore[attr-defined]
            tab_max_length=max_len,
            id=tab_id,
        )
        await tabs.add_pane(pane)
        tabs.active = tab_id

    @property
    def active_pane(self) -> TabPane | None:
        """Return the currently active TabPane (any type)."""
        tabs = self.query_one("#tabs", TabbedContent)
        return tabs.active_pane

    def post_feedback(self, text: str, severity: str = "information") -> None:
        """Post a UserFeedback message to the active tab pane."""
        pane = self.active_pane
        if pane is not None:
            pane.post_message(UserFeedback(text, severity))
        else:
            self.notify(text, severity=severity)

    async def _close_active_tab(self) -> None:
        """Close the active chat session tab (refuses to close the last one)."""
        tabs = self.query_one("#tabs", TabbedContent)
        pane_count = len(list(tabs.query(TabPane)))
        if pane_count <= 1:
            self.post_feedback("Cannot close the last tab. (Use ctrl+q to quit)", severity="warning")
            return
        active_id = tabs.active
        if active_id:
            tabs.remove_pane(active_id)

    def action_close_tab(self) -> None:
        self.run_worker(self._close_active_tab())

    def action_new_tab(self) -> None:
        self.run_worker(self._add_tab())

    def _switch_tab(self, delta: int) -> None:
        """Switch to the tab *delta* positions away (wrapping)."""
        tabs = self.query_one("#tabs", TabbedContent)
        panes = list(tabs.query(TabPane))
        if len(panes) <= 1:
            return
        ids = [p.id for p in panes]
        idx = ids.index(tabs.active)
        new_id = ids[(idx + delta) % len(ids)]
        tabs.active = new_id
        # Focus the new pane's chat input so the old pane's focused widget
        # doesn't cause TabbedContent to revert the switch.
        new_pane = tabs.get_pane(new_id)
        if isinstance(new_pane, ChatTabPane):
            workspace = new_pane.workspace
            chat_input = workspace.query_one("#chat-input")
            # When the input is gated (agent busy / interrupt pending), focus the chat area instead — it
            # owns the cancel binding and hosts any interrupt widgets the user needs to reach.
            if chat_input.disabled:
                workspace.query_one(ChatArea).focus()
            else:
                chat_input.focus()
        elif isinstance(new_pane, LogTabPane):
            new_pane.query_one("#log-output").focus()

    def action_next_tab(self) -> None:
        self._switch_tab(-1)

    def action_prev_tab(self) -> None:
        self._switch_tab(1)

    def action_focus_chat(self) -> None:
        """Focus the chat input in the active tab."""
        tabs = self.query_one("#tabs", TabbedContent)
        pane = tabs.active_pane
        if isinstance(pane, ChatTabPane):
            pane.workspace.query_one("#chat-input").focus()

