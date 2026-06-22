"""Guide tools — list and load on-demand reference material."""

from __future__ import annotations

from langchain.tools import tool
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command

from rhizome.agent_legacy.guides import GUIDE_REGISTRY
from rhizome.agent_legacy.tools.visibility import ToolVisibility, tool_visibility


def build_guide_tools() -> dict:
    """Build guide tools (list and load)."""

    @tool("list_guides", description=(
        "List all available guides with their names and descriptions. "
        "Use this to discover what reference material is available "
        "before loading a specific guide."
    ))
    @tool_visibility(ToolVisibility.LOW)
    async def list_guides_tool(runtime: ToolRuntime) -> Command:
        if not GUIDE_REGISTRY:
            return Command(update={
                "messages": [ToolMessage(
                    content="No guides available.",
                    tool_call_id=runtime.tool_call_id,
                )],
            })

        lines = [f"- **{g.name}**: {g.description}" for g in GUIDE_REGISTRY.values()]
        return Command(update={
            "messages": [ToolMessage(
                content=f"Available guides ({len(lines)}):\n" + "\n".join(lines),
                tool_call_id=runtime.tool_call_id,
            )],
        })

    @tool("read_guides", description=(
        "Load one or more guides by name, injecting their reference material "
        "into the conversation. Use list_guides to see what's available. "
        "Guides contain detailed instructions for specific workflows "
        "(e.g. crafting flashcards, commit proposals)."
    ))
    @tool_visibility(ToolVisibility.LOW)
    async def read_guides_tool(guide_names: list[str], runtime: ToolRuntime) -> Command:
        parts: list[str] = []
        errors: list[str] = []
        for name in guide_names:
            guide = GUIDE_REGISTRY.get(name)
            if guide is None:
                available = ", ".join(GUIDE_REGISTRY.keys()) or "(none)"
                errors.append(f"Guide {name!r} not found. Available: {available}")
            else:
                parts.append(f"[Guide: {guide.name}]\n\n{guide.content}")

        content = "\n\n---\n\n".join(parts) if parts else ""
        if errors:
            content = (content + "\n\n" if content else "") + "\n".join(errors)

        return Command(update={
            "messages": [ToolMessage(
                content=content,
                tool_call_id=runtime.tool_call_id,
            )],
        })

    return {
        "list_guides": list_guides_tool,
        "read_guides": read_guides_tool,
    }
