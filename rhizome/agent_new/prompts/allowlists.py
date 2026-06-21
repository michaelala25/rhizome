"""Per-mode tool allowlists, rendered into the <system> header injected at mode switches.

Every mode shares the same wire-level tool set — nothing is filtered out of the model request, which would
shuffle the prompt prefix and invalidate the cache. The allowlist is instead a behavioral contract: the
header block produced here tells the agent which tools the current mode permits, and the system prompt
instructs it to use only those. Tools may eventually also be gated on state (erroring when called out of
mode); that enforcement belongs to the tools themselves — ``MODE_ALLOWLISTS`` is the lookup they'd check.

Tool names refer to the root agent's tool set. Groups exist so headers render with a little structure
instead of one flat list; modes always include or exclude whole groups.
"""

TOOL_GROUPS: dict[str, tuple[str, ...]] = {
    "Knowledge base (read)": (
        "list_topics",
        "list_knowledge_entries",
        "read_knowledge_entries",
        "list_flashcards",
        "read_flashcards",
    ),
    "Knowledge base (write)": (
        "create_topics",
        "delete_topics",
    ),
    "App": (
        "update_app_state",
        "set_mode",
        "ask_user_input",
    ),
    "Commit workflow": (
        "commit_show_selected_messages",
        "commit_proposal_create",
        "commit_invoke_subagent",
        "commit_proposal_present",
        "commit_proposal_edit",
        "commit_proposal_accept",
    ),
    "Flashcard proposals": (
        "flashcard_proposal_create",
        "flashcard_proposal_present",
        "flashcard_proposal_edit",
        "flashcard_proposal_accept",
    ),
    "Review sessions": (
        "review_get_past_sessions",
        "review_show_session_state",
        "review_start_session",
        "review_update_session_state",
        "review_record_interaction",
        "review_present_flashcards",
        "review_finish_session",
    ),
    "Guides": (
        "list_guides",
        "read_guides",
    ),
    "Resources": (
        "query_resources",
    ),
    "Web": (
        "web_search",
        "web_fetch",
    ),
    "SQL (last resort)": (
        "execute_sql",
    ),
}
"""Tool names by display group, in render order."""

MODE_TOOL_GROUPS: dict[str, tuple[str, ...]] = {
    "idle": (
        "Knowledge base (read)",
        "Knowledge base (write)",
        "App",
        "Commit workflow",
        "Guides",
        "Resources",
        "Web",
        "SQL (last resort)",
    ),
    "learn": (
        "Knowledge base (read)",
        "Knowledge base (write)",
        "App",
        "Commit workflow",
        "Flashcard proposals",
        "Guides",
        "Resources",
        "Web",
        "SQL (last resort)",
    ),
    "review": (
        "Knowledge base (read)",
        "App",
        "Review sessions",
        "Flashcard proposals",
        "Guides",
        "Resources",
        "Web",
        "SQL (last resort)",
    ),
}
"""Group keys permitted per mode."""

MODE_ALLOWLISTS: dict[str, frozenset[str]] = {
    mode: frozenset(tool for group in groups for tool in TOOL_GROUPS[group])
    for mode, groups in MODE_TOOL_GROUPS.items()
}
"""Flat per-mode allowlists derived from the groups — the membership check for eventual state-gating."""


def render_tool_allowlist(mode: str) -> str:
    """Render the tool-allowlist block for a mode's <system> header message."""
    lines = [f"Tools permitted in **{mode}** mode:"]
    lines += [f"- {group}: {', '.join(TOOL_GROUPS[group])}" for group in MODE_TOOL_GROUPS[mode]]
    lines.append("Do not use any tool not listed above while this mode is active.")
    return "\n".join(lines)
