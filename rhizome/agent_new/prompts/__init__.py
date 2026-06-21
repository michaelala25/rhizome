"""Prompt content for the new agent stack: one fixed system prompt, mode/workflow guides injected as
context messages, and per-mode tool allowlists rendered into mode-switch headers."""

from .allowlists import MODE_ALLOWLISTS, MODE_TOOL_GROUPS, TOOL_GROUPS, render_tool_allowlist
from .guides import (
    CONVERSATIONAL_REVIEWS_GUIDE,
    DATABASE_SCHEMA_GUIDE,
    FLASHCARD_PROPOSAL_WORKFLOW_GUIDE,
    JUDGING_REVIEW_ANSWERS_GUIDE,
    KNOWLEDGE_ENTRIES_GUIDE,
    LEARN_MODE_GUIDE,
    LEARN_MODE_REMINDER,
    REVIEW_MODE_GUIDE,
    REVIEW_MODE_REMINDER,
    WRITING_GOOD_FLASHCARDS_GUIDE,
)
from .system import DEBUG_SECTION, SYSTEM_PROMPT, compose_system_prompt

__all__ = [
    "CONVERSATIONAL_REVIEWS_GUIDE",
    "DATABASE_SCHEMA_GUIDE",
    "DEBUG_SECTION",
    "FLASHCARD_PROPOSAL_WORKFLOW_GUIDE",
    "JUDGING_REVIEW_ANSWERS_GUIDE",
    "KNOWLEDGE_ENTRIES_GUIDE",
    "LEARN_MODE_GUIDE",
    "LEARN_MODE_REMINDER",
    "MODE_ALLOWLISTS",
    "MODE_TOOL_GROUPS",
    "REVIEW_MODE_GUIDE",
    "REVIEW_MODE_REMINDER",
    "SYSTEM_PROMPT",
    "TOOL_GROUPS",
    "WRITING_GOOD_FLASHCARDS_GUIDE",
    "compose_system_prompt",
    "render_tool_allowlist",
]
