# Core
from .view_base import ViewBase
from rhizome.app.vm import CallbackGroup, Emitter, ViewModelBase

# Shared widgets
from .shared.topic_tree import TopicTree

# Legacy widgets (in-place re-exports — sourced from .legacy)
from .legacy.agent_message_harness import AgentMessageHarness
from .legacy.chat_input import ChatInput
from .legacy.chat_pane import ChatPane, HintHigherVerbosity
from .legacy.command_palette import CommandPalette
from .legacy.entry_list import EntryList
from .legacy.explorer_viewer import ExplorerViewer
from .legacy.flashcard_list import FlashcardList
from .legacy.interrupt import InterruptWidgetBase
from .legacy.navigable import NavigableWidgetMixin, WidgetDeactivated
from .legacy.choices import Choices
from .legacy.multiple_choices import MultipleChoices
from .legacy.sql_confirmation import SqlConfirmation
from .legacy.warning import WarningChoices
from .legacy.message import ChatMessage, MarkdownChatMessage, RichChatMessage
from .legacy.options_editor import OptionsEditor
from .legacy.resource.linker import ResourceLinker
from .legacy.resource.list_view import ResourceList
from .legacy.resource.loader import ResourceLoader
from .legacy.resource.viewer import ResourceViewer
from .legacy.status_bar import StatusBar
from .legacy.thinking import Spinner, ThinkingIndicator
from .legacy.tool_call_list import ToolCallList

# Kept-in-place (slated for rewrite but still wired into both legacy and new code paths)
from .commit_proposal import CommitProposal
from .flashcard_proposal import FlashcardProposal
from .flashcard_review.view import FlashcardReview

__all__ = [
    "CallbackGroup",
    "Emitter",
    "ViewBase",
    "ViewModelBase",

    "AgentMessageHarness",
    "ChatInput",
    "ChatMessage",
    "ChatPane",
    "Choices",
    "CommandPalette",
    "CommitProposal",
    "EntryList",
    "FlashcardProposal",
    "ExplorerViewer",
    "FlashcardList",
    "FlashcardReview",
    "HintHigherVerbosity",
    "InterruptWidgetBase",
    "MarkdownChatMessage",
    "NavigableWidgetMixin",
    "MultipleChoices",
    "OptionsEditor",
    "ResourceLinker",
    "ResourceList",
    "ResourceLoader",
    "ResourceViewer",
    "RichChatMessage",
    "Spinner",
    "SqlConfirmation",
    "StatusBar",
    "ThinkingIndicator",
    "ToolCallList",
    "TopicTree",
    "WarningChoices",
    "WidgetDeactivated",
]
