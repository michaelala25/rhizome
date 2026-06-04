"""WelcomeMessage — view for ``WelcomeMessageVM``: the ASCII banner + greeting atop a fresh feed.

A dumb mirror of its (static) VM. ``ASCII_ART`` is pure presentation and lives here; the setup
screen renders it too.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widgets import Static

from rhizome.tui.widgets.view_base import ViewBase
from rhizome.app.chat_pane.welcome_message import WelcomeMessageVM
from rhizome.tui.widgets.chat_pane.feed_registry import register_feed_view


ASCII_ART = r"""
    ____  __    _
   / __ \/ /_  (_)___  ____  ____ ___  ___
  / /_/ / __ \/ /_  / / __ \/ __ `__ \/ _ \
 / _, _/ / / / / / /_/ /_/ / / / / / /  __/
/_/ |_/_/ /_/_/ /___/\____/_/ /_/ /_/\___/
"""


@register_feed_view(WelcomeMessageVM)
class WelcomeMessage(ViewBase[WelcomeMessageVM]):
    """ASCII art banner and welcome greeting shown at the top of the chat feed."""

    DEFAULT_CSS = """
    WelcomeMessage {
        layout: vertical;
        padding: 1 2;
        width: 1fr;
        height: auto;
        align: center middle;
        margin-bottom: 1;
        border: round rgb(80, 120, 90);
    }
    WelcomeMessage #welcome-art {
        content-align: center middle;
        width: 1fr;
        height: auto;
        color: $text-muted;
    }
    WelcomeMessage #welcome-title {
        text-align: center;
        width: 1fr;
        height: auto;
        color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(ASCII_ART, id="welcome-art")
        yield Static(self._vm.greeting, id="welcome-title")
