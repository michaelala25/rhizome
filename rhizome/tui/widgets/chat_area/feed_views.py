"""Registration manifest for chat-area feed views.


IMPORTING THIS MODULE IS WHAT POPULATES THE FEED-VIEW REGISTRY.
``@register_feed_view`` decorators only run when their module is imported, so the registry is
empty until something imports the views. This module is that something — importing it (the
chat area does, once) pulls in every native view for its decorator side effect and imperatively
registers the foreign widgets the chat area adopts. Import it before calling ``view_for``.

Adding a feed view? If it's chat-area-native, decorate it with ``@register_feed_view`` and import it
here. If it's a general widget the chat area merely displays in the feed, register it imperatively in the
"adopted" section below so the widget itself stays decoupled from the chat area.
"""

from __future__ import annotations

# Native views — imported for their ``@register_feed_view`` side effect.
from rhizome.tui.widgets.chat_area.messages.base import ChatMessage  # noqa: F401
from rhizome.tui.widgets.chat_area.messages.agent import AgentMessage  # noqa: F401
from rhizome.tui.widgets.chat_area.messages.tool import ToolMessage  # noqa: F401
from rhizome.tui.widgets.chat_area.messages.shell import ShellCommandMessage  # noqa: F401
from rhizome.tui.widgets.chat_area.thinking import ThinkingIndicator  # noqa: F401
from rhizome.tui.widgets.chat_area.welcome_message import WelcomeMessage  # noqa: F401
from rhizome.tui.widgets.chat_area.branch import BranchPoint  # noqa: F401
from rhizome.tui.widgets.chat_area.interrupts.test import TestInterrupt  # noqa: F401
from rhizome.tui.widgets.chat_area.interrupts.user_choices import UserChoices  # noqa: F401
from rhizome.tui.widgets.chat_area.interrupts.warning import WarningUserChoices  # noqa: F401
from rhizome.tui.widgets.chat_area.interrupts.multi_choices import MultiUserChoices  # noqa: F401
from rhizome.tui.widgets.chat_area.interrupts.sql import SqlConfirmation  # noqa: F401
from rhizome.tui.widgets.chat_area.interrupts.flashcard_review import FlashcardReviewInterrupt  # noqa: F401
from rhizome.tui.widgets.chat_area.interrupts.commit_proposal import CommitProposalInterrupt  # noqa: F401
from rhizome.tui.widgets.chat_area.interrupts.flashcard_proposal import FlashcardProposalInterrupt  # noqa: F401

# Adopted foreign widgets — general-purpose widgets the chat area also shows in the feed. Registered
# imperatively here so the widgets stay decoupled from the chat area (no decorator on their class).
from rhizome.app.browser.browser import BrowserModel
from rhizome.tui.widgets.browser.browser import Browser
from rhizome.app.options_editor import OptionsEditorModel
from rhizome.tui.widgets.options_editor import OptionsEditor
from rhizome.tui.widgets.chat_area.feed_registry import register_feed_view

register_feed_view(BrowserModel)(Browser)
register_feed_view(OptionsEditorModel)(OptionsEditor)
