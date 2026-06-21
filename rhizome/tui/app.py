"""Main Textual application."""

import logging
from datetime import datetime
from pathlib import Path

from textual import events
from textual.app import App
from textual.binding import Binding

import rhizome.tui.graphics as graphics

from rhizome.config import get_default_db_path
from rhizome.credentials import APIKeyService, CredentialsAPIKeyService
from rhizome.logs import get_logger, initialize_global_logger
from rhizome.tui.log_handler import TUILogHandler
from rhizome.app.options import Options, OptionScope, OptionService
from rhizome.db import SessionFactoryService, get_engine, get_session_factory
from rhizome.app.sql_session import NotifyingSessionFactory
from rhizome.utils.services import ServiceAccessor
from rhizome.utils.workers import WorkerSchedulerService
from rhizome.tui.screens.main import MainScreen, ChatTabPane, LogTabPane
from rhizome.tui.screens.setup import SetupScreen
from rhizome.tui.types import DatabaseCommitted


PROFILE_DIR = Path("/tmp/rhizome-profiles")


class RhizomeApp(App):
    """Rhizome TUI — a chat-based interface for learning and review."""

    TITLE = "rhizome"

    BINDINGS = [
        # Developer tool (toggles the pyinstrument profiler). Private + hidden from the HelpPanel.
        Binding("ctrl+f12", "toggle_profile", "Toggle profiler", id="app._toggle_profile",
                show=False, system=True, priority=True),
    ]

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
    ) -> None:
        super().__init__()
        self.debug_logging = debug
        self._profiler = None  # type: ignore[assignment]
        engine = get_engine(db_path or get_default_db_path())
        # Root service container. App-scoped dependencies are registered here and threaded down the
        # VM spine as a single accessor; child scopes (per session / per widget) shadow individual
        # services without touching this root.
        self.services = ServiceAccessor()
        self.services.register(
            SessionFactoryService,
            NotifyingSessionFactory(
                get_session_factory(engine),
                on_commit=lambda tables: self._notify_database_committed(tables),
            ),
        )
        # Scope-less fallback worker scheduler. Scope-owner VMs shadow this in their own child scope;
        # this root holder is non-bindable, so a view that binds here (because its VM never opened a
        # scoped scheduler) fails loudly instead of clobbering a single global binding.
        self.services.register(WorkerSchedulerService, WorkerSchedulerService(bindable=False))
        self.options: Options = Options.load()
        # Root options service. Per-conversation child scopes shadow this with a Session node parented
        # here, reachable via ``at_scope``; the root scope dispenses only Root. ``Options`` satisfies the
        # ``OptionService`` protocol directly, so the bare instance is registered under the key.
        self.services.register(OptionService, self.options)
        # API keys (injected resolver) + the optional embedding service. EmbeddingService is registered
        # only when a provider is configured, so consumers that ``try_get`` it degrade gracefully when
        # embeddings aren't set up. The Voyage impl is deferred-imported so its REST/splitter deps stay
        # off the startup path unless a key is present.
        api_keys = CredentialsAPIKeyService()
        self.services.register(APIKeyService, api_keys)
        if api_keys.has("voyage"):
            from rhizome.resources.embeddings import EmbeddingService, VoyageEmbedder
            self.services.register(EmbeddingService, VoyageEmbedder(api_keys))
        self.options.subscribe_on_changed(Options.Theme, self._on_theme_changed)
        self.options.subscribe_on_changed(Options.TabMaxLength, self._on_tab_max_length_changed)
        self.theme = self.options.get(Options.Theme)

        # Set up in-app log handler for the rhizome logger
        self.tui_log_handler = TUILogHandler()
        self.tui_log_handler.setLevel(logging.DEBUG)
        initialize_global_logger(self.tui_log_handler)

        # REMARK: _logger is a reserved name in textual.App, which we can't override ourselves, so we use _log instead.
        self._log = get_logger("tui.app")
        self._log.info("App initialized (db=%s)", db_path or get_default_db_path())

    def _on_theme_changed(self, old: str, new: str) -> None:
        self._log.info("Theme changed: %s → %s", old, new)
        self.theme = new

    def _on_tab_max_length_changed(self, old: int, new: int) -> None:
        for pane in self.screen.query(ChatTabPane):
            pane.update_tab_max_length(new)
        for pane in self.screen.query(LogTabPane):
            pane.update_tab_max_length(new)

    def on_mount(self) -> None:
        if not self.services.get(APIKeyService).has("anthropic"):
            self.push_screen(SetupScreen(), callback=self._on_setup_complete)
        else:
            self.push_screen(MainScreen())

    def on_resize(self, event: events.Resize) -> None:
        # Keep the graphics cell size live (off the whole-window grid) so terminal-rendered images — the
        # math blocks in agent messages — re-rasterize at the right scale on a font-zoom. No-op without a
        # backend or when the terminal doesn't report pixel size.
        graphics.note_resize(self.size.width, self.size.height, event.pixel_size)

    def _on_setup_complete(self, completed: bool) -> None:
        self.push_screen(MainScreen())

    def _notify_database_committed(self, tables: frozenset[str]) -> None:
        """A DB commit occurred — propagate to the active screen."""
        event = DatabaseCommitted(tables)
        screen = self.screen
        if isinstance(screen, MainScreen):
            screen.notify_database_committed(event)

    # ------------------------------------------------------------------
    # Profiling (ctrl+f12 toggles a pyinstrument session)
    # ------------------------------------------------------------------

    def action_toggle_profile(self) -> None:
        from pyinstrument import Profiler
        from rhizome.tui._profiling import (
            start_stylesheet_instrumentation,
            stop_stylesheet_instrumentation,
        )

        if self._profiler is None:
            self._profiler = Profiler(async_mode="enabled")
            start_stylesheet_instrumentation()
            self._profiler.start()
            self.notify("Profiling started", severity="warning", timeout=2)
            return

        self._profiler.stop()
        stylesheet_report = stop_stylesheet_instrumentation()
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        html_out = PROFILE_DIR / f"profile-{stamp}.html"
        txt_out = PROFILE_DIR / f"profile-{stamp}-stylesheet.txt"
        html_out.write_text(self._profiler.output_html())
        txt_out.write_text(stylesheet_report)
        self._profiler = None
        self.notify(
            f"Profile written: {html_out.name} + .txt",
            severity="information", timeout=5,
        )
