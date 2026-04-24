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
