"""Tool infrastructure and builders for the rhizome agent.

Re-exports
----------
- ``ToolVisibility``, ``TOOL_VISIBILITY``, ``tool_visibility`` — visibility system

Domain-specific tool builders live in submodules:
- ``tools.core`` — core knowledge-base tools (topics, entries, flashcard lookup)
- ``tools.app`` — app control tools (mode switching, tab renaming, etc.)
- ``tools.sql`` — SQL exploration/modification tools
- ``tools.flashcard_proposal`` — flashcard proposal tools
- ``tools.review`` — review session state machine tools
"""

from rhizome.agent_legacy.tools.visibility import TOOL_VISIBILITY, ToolVisibility, tool_visibility

__all__ = [
    "TOOL_VISIBILITY",
    "ToolVisibility",
    "tool_visibility",
]
