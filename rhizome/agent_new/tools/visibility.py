"""Tool visibility — a descriptive ``name -> level`` registry controlling which tool calls surface in
the chat display, and at what verbosity.

This is metadata, not state: a tool's visibility is static and the same for every conversation. The
``tool_visibility`` decorator stacks above ``@tool`` (it reads the tool's ``.name``) and records the
level as a build-time side effect; the display layer looks levels up *by name* — the only handle it has,
since tool calls arrive off the stream as ``{"name", "input"}`` blocks — and filters against the current
verbosity threshold.
"""

from enum import IntEnum

from rhizome.logs import get_logger

_logger = get_logger("agent_new.tools.visibility")


class ToolVisibility(IntEnum):
    LOW = 0       # Housekeeping tools (set_mode, rename_tab) — only visible at max verbosity
    DEFAULT = 1   # Most tools — visible at normal verbosity
    HIGH = 2      # Important tools — always visible


TOOL_VISIBILITY: dict[str, ToolVisibility] = {
    # Anthropic server-side tools (registered here since they're dicts, not decorated functions).
    "web_search": ToolVisibility.DEFAULT,
    "web_fetch": ToolVisibility.DEFAULT,
}


def tool_visibility(level: ToolVisibility):
    """Decorator that registers a tool's visibility level under its name. Apply it above ``@tool`` so the
    tool's ``.name`` is set by the time this reads it."""
    def decorator(func):
        name = getattr(func, "name", None) or func.__name__
        if name not in TOOL_VISIBILITY:
            TOOL_VISIBILITY[name] = level
        elif TOOL_VISIBILITY[name] != level:
            _logger.info(
                f"A new tool closure '{name}' has a different visibility level specified than a previous "
                f"one. Previous: {TOOL_VISIBILITY[name]}, new: {level}."
            )
        return func
    return decorator
