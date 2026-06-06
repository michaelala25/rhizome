"""Registration manifest for chat-pane feed views.


IMPORTING THIS MODULE IS WHAT POPULATES THE FEED-VIEW REGISTRY.
``@register_feed_view`` decorators only run when their module is imported, so the registry is
empty until something imports the views. This module is that something — importing it (the
pane does, once) pulls in every native view for its decorator side effect and imperatively
registers the foreign widgets the pane adopts. Import it before calling ``view_for``.

Adding a feed view? If it's chat-pane-native, decorate it with ``@register_feed_view`` and import it
here. If it's a general widget the pane merely displays in the feed, register it imperatively in the
"adopted" section below so the widget itself stays decoupled from the chat pane.
"""

from __future__ import annotations

# Native views — imported for their ``@register_feed_view`` side effect.
from rhizome.tui.widgets.chat_pane.messages.base import ChatMessage  # noqa: F401
from rhizome.tui.widgets.chat_pane.messages.agent import AgentMessage  # noqa: F401
from rhizome.tui.widgets.chat_pane.messages.tool import ToolMessage  # noqa: F401
from rhizome.tui.widgets.chat_pane.messages.shell import ShellCommandMessage  # noqa: F401
from rhizome.tui.widgets.chat_pane.thinking import ThinkingIndicator  # noqa: F401
from rhizome.tui.widgets.chat_pane.welcome_message import WelcomeMessage  # noqa: F401
from rhizome.tui.widgets.chat_pane.branch import BranchPoint  # noqa: F401
from rhizome.tui.widgets.chat_pane.interrupts.test import TestInterrupt  # noqa: F401
from rhizome.tui.widgets.chat_pane.interrupts.user_choices import UserChoices  # noqa: F401
from rhizome.tui.widgets.chat_pane.interrupts.warning import WarningUserChoices  # noqa: F401
from rhizome.tui.widgets.chat_pane.interrupts.multi_choices import MultiUserChoices  # noqa: F401
from rhizome.tui.widgets.chat_pane.interrupts.sql import SqlConfirmation  # noqa: F401
from rhizome.tui.widgets.chat_pane.interrupts.flashcard_review import FlashcardReviewInterrupt  # noqa: F401
from rhizome.tui.widgets.chat_pane.interrupts.commit_proposal import CommitProposalInterrupt  # noqa: F401
from rhizome.tui.widgets.chat_pane.interrupts.flashcard_proposal import FlashcardProposalInterrupt  # noqa: F401

# Adopted foreign widgets — general-purpose widgets the pane also shows in the feed. Registered
# imperatively here so the widgets stay decoupled from the chat pane (no decorator on their class).
from rhizome.app.browser.browser import BrowserModel
from rhizome.tui.widgets.browser.browser import Browser
from rhizome.app.options_editor import OptionsEditorModel
from rhizome.tui.widgets.options_editor import OptionsEditor
from rhizome.tui.widgets.chat_pane.feed_registry import register_feed_view

register_feed_view(BrowserModel)(Browser)
register_feed_view(OptionsEditorModel)(OptionsEditor)
