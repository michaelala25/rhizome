"""Agent operating modes — control the system prompt and tool visibility.

Each mode defines an allowlist of tools and composes a system prompt from
shared and mode-specific sections.  The ``AgentModeMiddleware`` reads the
active mode on every LLM call and overrides the request accordingly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from rhizome.agent.system_prompt import (
    DEBUG_SECTION,
    IDLE_MODE_SECTION,
    LEARN_MODE_SECTION,
    REVIEW_MODE_SECTION,
    SHARED_PREAMBLE,
)


# -- Shared tool groups -----------------------------------------------------
# These constants keep the allowlists DRY.  When a new tool is added, add it
# to the appropriate group(s) here.

_DB_READ_TOOLS = frozenset({
    "list_topics",
    "list_knowledge_entries",
    "read_knowledge_entries",
    "list_flashcards",
    "read_flashcards",
})

_DB_WRITE_TOOLS = frozenset({
    "create_topics",
    "delete_topics",
})

_APP_TOOLS = frozenset({
    "update_app_state",
    "set_mode",
    "ask_user_input",
})

_COMMIT_TOOLS = frozenset({
    "commit_show_selected_messages",
    "commit_proposal_create",
    "commit_invoke_subagent",
    "commit_proposal_present",
    "commit_proposal_edit",
    "commit_proposal_accept",
})

_WEB_TOOLS = frozenset({
    "web_search",
    "web_fetch",
})

_DB_SQL_TOOLS = frozenset({
    "execute_sql",
})

_FLASHCARD_PROPOSAL_TOOLS = frozenset({
    "flashcard_proposal_create",
    "flashcard_proposal_present",
    "flashcard_proposal_edit",
    "flashcard_proposal_accept",
})

_GUIDE_TOOLS = frozenset({
    "list_guides",
    "read_guides",
})

_RESOURCE_TOOLS = frozenset({
    # "add_resource",
    # "list_resources",
    # "get_resource_info",
    "query_resources",
})

_REVIEW_TOOLS = frozenset({
    "review_get_past_sessions",
    "review_show_session_state",
    "review_start_session",
    "review_update_session_state",
    "review_record_interaction",
    "review_present_flashcards",
    "review_finish_session",
})


def _compose_prompt(*sections: str) -> str:
    return "".join(sections)


# -- Base class --------------------------------------------------------------

class AgentMode(ABC):
    """Defines the system prompt and tool allowlist for an agent operating mode."""

    def __init__(self, *, debug: bool = False) -> None:
        self._debug = debug

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier (matches ``Mode`` enum values)."""

    @property
    @abstractmethod
    def system_prompt(self) -> str:
        """Complete system prompt for this mode."""

    @property
    @abstractmethod
    def allowed_tools(self) -> frozenset[str]:
        """Set of tool names visible to the LLM in this mode."""

    def is_tool_allowed(self, tool_name: str) -> bool:
        return tool_name in self.allowed_tools


# -- Concrete modes ----------------------------------------------------------

class IdleAgentMode(AgentMode):
    """Default mode — the user hasn't entered a specific workflow yet."""

    @property
    def name(self) -> str:
        return "idle"

    @property
    def system_prompt(self) -> str:
        return _compose_prompt(
            SHARED_PREAMBLE,
            IDLE_MODE_SECTION,
            *(DEBUG_SECTION,) if self._debug else (),
        )

    @property
    def allowed_tools(self) -> frozenset[str]:
        return _DB_READ_TOOLS   | \
               _DB_WRITE_TOOLS  | \
               _APP_TOOLS       | \
               _COMMIT_TOOLS    | \
               _WEB_TOOLS       | \
               _DB_SQL_TOOLS    | \
               _GUIDE_TOOLS     | \
               _RESOURCE_TOOLS


class LearnAgentMode(AgentMode):
    """Active during learning — teaching, grounding in the KB, and commits."""

    @property
    def name(self) -> str:
        return "learn"

    @property
    def system_prompt(self) -> str:
        return _compose_prompt(
            SHARED_PREAMBLE,
            LEARN_MODE_SECTION,
            *(DEBUG_SECTION,) if self._debug else (),
        )

    @property
    def allowed_tools(self) -> frozenset[str]:
        return _DB_READ_TOOLS            | \
               _DB_WRITE_TOOLS           | \
               _APP_TOOLS                | \
               _COMMIT_TOOLS             | \
               _FLASHCARD_PROPOSAL_TOOLS | \
               _WEB_TOOLS                | \
               _DB_SQL_TOOLS             | \
               _GUIDE_TOOLS              | \
               _RESOURCE_TOOLS


class ReviewAgentMode(AgentMode):
    """Active during review/quiz sessions — full review state machine."""

    @property
    def name(self) -> str:
        return "review"

    @property
    def system_prompt(self) -> str:
        return _compose_prompt(
            SHARED_PREAMBLE,
            REVIEW_MODE_SECTION,
            *(DEBUG_SECTION,) if self._debug else (),
        )

    @property
    def allowed_tools(self) -> frozenset[str]:
        return _DB_READ_TOOLS            | \
               _APP_TOOLS                | \
               _WEB_TOOLS                | \
               _REVIEW_TOOLS             | \
               _FLASHCARD_PROPOSAL_TOOLS | \
               _DB_SQL_TOOLS             | \
               _GUIDE_TOOLS              | \
               _RESOURCE_TOOLS


# -- Registry ----------------------------------------------------------------

MODE_REGISTRY: dict[str, type[AgentMode]] = {
    "idle": IdleAgentMode,
    "learn": LearnAgentMode,
    "review": ReviewAgentMode,
}

__all__ = [
    "AgentMode",
    "IdleAgentMode",
    "LearnAgentMode",
    "MODE_REGISTRY",
    "ReviewAgentMode",
]
