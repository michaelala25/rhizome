"""Main Textual application."""

import logging
from pathlib import Path

from textual import messages
from textual.app import App
from textual.widgets import TabbedContent

from rhizome.config import get_default_db_path
from rhizome.credentials import has_api_key
from rhizome.logs import get_logger, initialize_global_logger
from rhizome.tui.log_handler import TUILogHandler
from rhizome.app.options import Options, OptionScope
from rhizome.db import get_engine, get_session_factory
from rhizome.app.sql_session import NotifyingSessionFactory
from rhizome.tui.screens.main import MainScreen, ChatTabPane, LogTabPane
from rhizome.tui.screens.setup import SetupScreen
from rhizome.tui.types import DatabaseCommitted
from rhizome.tui.widgets import ChatPane


class RhizomeApp(App):
    """Rhizome TUI — a chat-based interface for learning and review."""

    TITLE = "rhizome"

    CSS = """
    MainScreen {
        background: $surface;
    }
    .navigable {
        border: solid rgb(40,40,40);
    }
    .navigable:hover {
        border: solid rgb(120,120,120);
    }
    .navigable:focus-within {
        border: solid rgb(86,126,160);
    }
    .deactivated {
        border: solid rgb(30,30,30);
    }
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        debug: bool = False,
        new_chat_pane: bool = False,
    ) -> None:
        super().__init__()
        self.debug_logging = debug
        # Temporary: opt-in to the MVVM chat-pane rewrite. Read by MainScreen /
        # ChatTabPane to pick which widget to mount.
        self.new_chat_pane = new_chat_pane
        engine = get_engine(db_path or get_default_db_path())
        self.session_factory = NotifyingSessionFactory(
            get_session_factory(engine),
            on_commit=lambda tables: self._notify_database_committed(tables),
        )
        self.options: Options = Options.load()
        self.options.subscribe(Options.Theme, self._on_theme_changed)
        self.options.subscribe(Options.TabMaxLength, self._on_tab_max_length_changed)
        self.theme = self.options.get(Options.Theme)

        # Set up in-app log handler for the rhizome logger
        self.tui_log_handler = TUILogHandler()
        self.tui_log_handler.setLevel(logging.DEBUG)
        initialize_global_logger(self.tui_log_handler)

        # REMARK: _logger is a reserved name in textual.App, which we can't override ourselves, so we use _log instead.
        self._log = get_logger("tui.app")
        self._log.info("App initialized (db=%s)", db_path or get_default_db_path())

    async def _on_theme_changed(self, old: str, new: str) -> None:
        self._log.info("Theme changed: %s → %s", old, new)
        self.theme = new

    async def _on_tab_max_length_changed(self, old: int, new: int) -> None:
        for pane in self.screen.query(ChatTabPane):
            pane.update_tab_max_length(new)
        for pane in self.screen.query(LogTabPane):
            pane.update_tab_max_length(new)

    def on_mount(self) -> None:
        if not has_api_key("anthropic"):
            self.push_screen(SetupScreen(), callback=self._on_setup_complete)
        else:
            self.push_screen(MainScreen())

    def _on_setup_complete(self, completed: bool) -> None:
        self.push_screen(MainScreen())

    def _notify_database_committed(self, tables: frozenset[str]) -> None:
        """A DB commit occurred — propagate to the active screen."""
        event = DatabaseCommitted(tables)
        screen = self.screen
        if isinstance(screen, MainScreen):
            screen.notify_database_committed(event)

    def on_exit_app(self, event: messages.ExitApp) -> None:
        for pane in self.query(ChatPane):
            pane._close_agent_log()

    @property
    def active_chat_pane(self) -> ChatPane | None:
        """Return the ChatPane in the currently active tab."""
        tabs = self.screen.query_one("#tabs", TabbedContent)
        active = tabs.active_pane
        if active is None:
            return active
        return active.query_one(ChatPane)
