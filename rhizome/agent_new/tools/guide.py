"""Guide tools — list and load on-demand reference material.

The loadable guides live as prompt content (``rhizome.agent_new.prompts.guides.LOADABLE_GUIDES``); these
tools are the agent's handle on them. ``list_guides`` surfaces what's available; ``read_guides`` injects a
guide's full content as the tool result, so it lands in the conversation history. Mode guides are not
loadable here — the prompt engine injects those automatically on a mode switch.
"""

from langchain.tools import tool

from ..prompts.guides import LOADABLE_GUIDES
from .visibility import ToolVisibility, tool_visibility

_LIST_GUIDES_DESC = (
    "List all available guides with their names and descriptions. Use this to discover what reference "
    "material is available before loading a specific guide."
)

_READ_GUIDES_DESC = (
    "Load one or more guides by name, injecting their reference material into the conversation. Use "
    "list_guides to see what's available. Guides contain detailed instructions for specific workflows "
    "(e.g. crafting flashcards, commit proposals)."
)


def build_guide_tools() -> dict:
    """Build the guide tools (name -> tool), following the ``build_*_tools`` convention."""

    @tool_visibility(ToolVisibility.LOW)
    @tool("list_guides", description=_LIST_GUIDES_DESC)
    async def list_guides_tool() -> str:
        if not LOADABLE_GUIDES:
            return "No guides available."
        lines = [f"- **{g.name}**: {g.description}" for g in LOADABLE_GUIDES.values()]
        return f"Available guides ({len(lines)}):\n" + "\n".join(lines)

    @tool_visibility(ToolVisibility.LOW)
    @tool("read_guides", description=_READ_GUIDES_DESC)
    async def read_guides_tool(guide_names: list[str]) -> str:
        parts: list[str] = []
        errors: list[str] = []
        for name in guide_names:
            guide = LOADABLE_GUIDES.get(name)
            if guide is None:
                available = ", ".join(LOADABLE_GUIDES) or "(none)"
                errors.append(f"Guide {name!r} not found. Available: {available}")
            else:
                parts.append(f"[Guide: {guide.name}]\n\n{guide.content}")

        content = "\n\n---\n\n".join(parts)
        if errors:
            content = (content + "\n\n" if content else "") + "\n".join(errors)
        return content

    return {"list_guides": list_guides_tool, "read_guides": read_guides_tool}
