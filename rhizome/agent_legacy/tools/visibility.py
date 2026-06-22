"""Tool visibility system — controls which tools appear in the TUI status bar."""

from enum import IntEnum

from rhizome.logs import get_logger

_logger = get_logger("agent.tools.visibility")


class ToolVisibility(IntEnum):
    LOW = 0       # Housekeeping tools (set_mode, rename_tab) — only visible at max verbosity
    DEFAULT = 1   # Most tools — visible at normal verbosity
    HIGH = 2      # Important tools — always visible

TOOL_VISIBILITY: dict[str, ToolVisibility] = {
    # Anthropic server-side tools (registered here since they're dicts, not decorated functions)
    "web_search": ToolVisibility.DEFAULT,
    "web_fetch": ToolVisibility.DEFAULT,
}

def tool_visibility(level: ToolVisibility):
    """Decorator that registers a tool's visibility level."""
    def decorator(func):
        name = getattr(func, 'name', None) or func.__name__
        if name not in TOOL_VISIBILITY:
            TOOL_VISIBILITY[name] = level
        elif TOOL_VISIBILITY[name] != level:
            _logger.info(
                f"A new tool closure '{name}' has a different visibility level specified than a previous one. "
                f"Previous: {TOOL_VISIBILITY[name]}, new: {level}."
            )
        return func
    return decorator
