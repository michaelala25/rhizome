"""Runtime context passed to every tool invocation."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentContext:
    user_settings: dict = field(default_factory=dict)
    """Dynamic user settings injected into model calls via middleware."""

    answerer_subagent: Any = None
    """Flashcard answerer subagent — produces a candidate answer for a prompt."""

    comparator_subagent: Any = None
    """Flashcard comparator subagent — compares a candidate answer to the expected one."""

    scorer_subagent: Any = None
    """Flashcard scorer subagent — assigns a score to a flashcard review."""

    commit_subagent: Any = None
    """Commit subagent — drives the commit-proposal workflow."""

    session_factory: Any = None
    """DB session factory — widgets that write to the DB (e.g. the
    flashcard review widget invoking ``apply_rating``) pull this off the
    context when constructed from an interrupt."""

    conversation_cursor: Any = None
    """ConversationGraphCursor pinned at turn start, identifying the chat-pane branch
    this turn was launched from. Tools that mutate per-branch state (currently
    ``update_app_state(current_branch_name=...)``) read this and forward it to the
    chat pane so the mutation addresses the launching branch even if the user has
    navigated elsewhere mid-stream. Typed ``Any`` to keep tui types out of this
    module — the value is opaque to the agent layer."""
