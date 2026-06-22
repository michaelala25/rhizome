"""Commit subagent: proposes knowledge entries from selected conversation messages."""

import json
from typing import Any

from langchain.agents.middleware.types import AgentState
from langchain_core.messages import ToolMessage
from pydantic import BaseModel, Field
from langchain.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command, interrupt

from rhizome.agent_legacy.builder import build_agent
from rhizome.agent_legacy.state import CommitProposalEntry, CommitProposalState
from rhizome.agent_legacy.subagents.base import Subagent
from rhizome.agent_legacy.guides import GUIDE_REGISTRY
from rhizome.agent_legacy.tools.core import build_core_tools
from rhizome.agent_legacy.tools.visibility import ToolVisibility, tool_visibility
from rhizome.db.models import EntryType
from rhizome.db.operations import create_entry, get_topic
from rhizome.logs import get_logger
from rhizome.tui.commit_state import CommitApproved

_logger = get_logger("agent.commit")

COMMIT_SYSTEM_PROMPT = """\
You are a knowledge extraction assistant for a knowledge management system.

Given a set of conversation messages from a learning session, your task is to propose
structured knowledge entries to commit to the database. Each entry should capture a
discrete, self-contained piece of knowledge from the conversation.

You have access to database tools to query existing topics and entries so you can:
- Determine which topic_id to assign each entry to
- Avoid creating duplicate entries
- Understand the existing knowledge structure

""" + GUIDE_REGISTRY["knowledge_entries"].content + """

## How to propose entries

Use the `stage_entries` tool to create your initial proposal. Each entry needs:
- `title`: short descriptive title
- `content`: full content of the knowledge entry
- `entry_type`: one of "fact", "exposition", or "overview"
- `topic_id`: integer topic ID (use database tools to find the right one)

If you are revising an existing proposal (e.g. after user feedback), use `edit_entries`
to make targeted changes by stable ID. Do NOT use `stage_entries` to replace the entire
proposal — that would discard user edits.

Once you have staged or edited entries, respond with a brief summary of what you proposed
or changed. Do NOT include the full entry content in your response.
"""


# ---------------------------------------------------------------------------
# Pydantic schemas (used by both subagent and root-agent tools)
# ---------------------------------------------------------------------------

class KnowledgeEntryProposalSchema(BaseModel):
    title: str
    content: str
    entry_type: str
    topic_id: int


class CommitEntryEdit(BaseModel):
    """Partial update to a single entry in the commit proposal."""
    id: int = Field(description="Stable ID of the entry to edit")
    title: str | None = Field(default=None, description="New title (omit to keep current)")
    content: str | None = Field(default=None, description="New content (omit to keep current)")
    entry_type: str | None = Field(default=None, description="New entry type: fact, exposition, or overview (omit to keep current)")
    topic_id: int | None = Field(default=None, description="New topic ID (omit to keep current)")


# ---------------------------------------------------------------------------
# Subagent state
# ---------------------------------------------------------------------------

class CommitSubagentState(AgentState):
    """State for the commit subagent graph, mirroring the root agent's
    commit_proposal field so the subagent can operate on the same data."""
    commit_proposal: list[CommitProposalEntry]


# ---------------------------------------------------------------------------
# Diff helper
# ---------------------------------------------------------------------------

def _build_commit_diff(
    original: list[dict],
    returned: list[dict],
    originals_by_id: dict[int, dict],
) -> list[str]:
    """Compare original proposal entries against widget-returned entries.

    Returns a list of human-readable lines describing exclusions and edits.
    """
    returned_ids = {e["id"] for e in returned}
    original_ids = {e["id"] for e in original}

    parts: list[str] = []

    # Exclusions
    excluded_ids = sorted(original_ids - returned_ids)
    if excluded_ids:
        labels = [f"entry {eid} ({originals_by_id[eid]['title']!r})" for eid in excluded_ids]
        parts.append(f"Excluded by user: {', '.join(labels)}")

    # Per-entry edits
    for entry in returned:
        entry_id = entry["id"]
        orig = originals_by_id[entry_id]
        changed: list[str] = []
        if entry["title"] != orig["title"]:
            changed.append("title")
        if entry["content"] != orig["content"]:
            changed.append("content")
        if entry["entry_type"] != orig["entry_type"]:
            changed.append("entry_type")
        if entry["topic_id"] != orig["topic_id"]:
            changed.append("topic_id")
        if changed:
            parts.append(f"Entry {entry_id}: user edited {', '.join(changed)}")

    if not parts:
        parts.append("No direct edits or exclusions by user.")

    return parts


# ---------------------------------------------------------------------------
# Subagent builder
# ---------------------------------------------------------------------------

def _build_subagent_tools():
    """Build tools that the commit subagent uses internally to modify its own
    proposal state.  These are NOT exposed to the root agent."""

    @tool("stage_entries", description=(
        "Stage a fresh set of knowledge entries as the commit proposal. "
        "Use this for initial proposal creation. Each entry is auto-assigned "
        "a stable ID. Do NOT use this to revise an existing proposal — use "
        "edit_entries instead to preserve user edits."
    ))
    async def stage_entries(
        entries: list[KnowledgeEntryProposalSchema],
        runtime: ToolRuntime,
    ) -> Command:
        if not entries:
            return Command(update={
                "messages": [ToolMessage(
                    content="Error: no entries provided.",
                    tool_call_id=runtime.tool_call_id,
                )],
            })

        proposal = [
            CommitProposalEntry(
                id=i, title=e.title, content=e.content,
                entry_type=e.entry_type, topic_id=e.topic_id,
            )
            for i, e in enumerate(entries)
        ]
        msg = f"Staged {len(proposal)} entry/entries."
        return Command(update={
            "commit_proposal": proposal,
            "messages": [ToolMessage(content=msg, tool_call_id=runtime.tool_call_id)],
        })

    @tool("edit_entries", description=(
        "Make targeted edits to the current commit proposal. "
        "Supports in-place edits (partial field updates by stable ID), "
        "deletions (by ID), and additions (auto-assigned IDs). "
        "Processing order: edits, then deletions, then additions."
    ))
    async def edit_entries(
        runtime: ToolRuntime,
        edits: list[CommitEntryEdit] | None = None,
        additions: list[KnowledgeEntryProposalSchema] | None = None,
        deletions: list[int] | None = None,
    ) -> Command:
        proposal = runtime.state.get("commit_proposal") or []
        if not proposal and not additions:
            return Command(update={
                "messages": [ToolMessage(
                    content="Error: no proposal to edit and no additions provided.",
                    tool_call_id=runtime.tool_call_id,
                )],
            })

        entries = [dict(e) for e in proposal]
        entries_by_id = {e["id"]: e for e in entries}
        changes: list[str] = []

        for edit in (edits or []):
            entry = entries_by_id.get(edit.id)
            if entry is None:
                continue
            if edit.title is not None:
                entry["title"] = edit.title
            if edit.content is not None:
                entry["content"] = edit.content
            if edit.entry_type is not None:
                entry["entry_type"] = edit.entry_type
            if edit.topic_id is not None:
                entry["topic_id"] = edit.topic_id
            changes.append(f"edited entry {edit.id}")

        delete_ids = set(deletions or [])
        for did in sorted(delete_ids):
            if did in entries_by_id:
                changes.append(f"deleted entry {did} ({entries_by_id[did]['title']!r})")
        entries = [e for e in entries if e["id"] not in delete_ids]

        next_id = max((e["id"] for e in proposal), default=-1) + 1
        for addition in (additions or []):
            entries.append(CommitProposalEntry(
                id=next_id, title=addition.title, content=addition.content,
                entry_type=addition.entry_type, topic_id=addition.topic_id,
            ))
            changes.append(f"added entry {next_id} ({addition.title!r})")
            next_id += 1

        new_proposal = [CommitProposalEntry(**e) for e in entries]
        summary = "; ".join(changes) if changes else "no changes applied"
        msg = f"Proposal updated ({len(new_proposal)} entry/entries): {summary}."
        return Command(update={
            "commit_proposal": new_proposal,
            "messages": [ToolMessage(content=msg, tool_call_id=runtime.tool_call_id)],
        })

    return [stage_entries, edit_entries]


def build_commit_subagent(session_factory, chat_pane, **agent_kwargs) -> Subagent:
    """Build the commit subagent with DB tools and proposal-editing tools.

    Returns a ``Subagent`` whose graph uses ``CommitSubagentState`` so its
    tools can read/write ``commit_proposal`` via ``extra_state``.
    """
    db_tools = list(build_core_tools(session_factory).values())
    proposal_tools = _build_subagent_tools()

    provider = agent_kwargs.pop("provider", "anthropic")
    model_name = agent_kwargs.pop("model_name", "claude-sonnet-4-6")

    model, agent, _middleware = build_agent(
        db_tools + proposal_tools,
        provider=provider,
        model_name=model_name,
        name="commit",
        state_schema=CommitSubagentState,
        **{**agent_kwargs, "temperature": 0.1},
    )

    return Subagent(
        model=model,
        agent=agent,
        system_prompt=COMMIT_SYSTEM_PROMPT,
        stateful=True,
    )


# ---------------------------------------------------------------------------
# Root-agent tools (invoke subagent, direct create, present, edit, accept)
# ---------------------------------------------------------------------------

def build_commit_subagent_tools(
    session_factory,
    chat_pane,
    subagent: Subagent,
) -> list:
    """Build the tools the root agent sees for the commit workflow.

    These tools allow the root agent to invoke the commit subagent or
    propose entries directly, present proposals to the user, and write
    approved entries to the DB.  Proposal and payload state are stored
    in ``RhizomeAgentState`` (``commit_proposal`` and ``commit_payload``
    fields) and accessed via ``ToolRuntime``.
    """

    @tool("commit_show_selected_messages", description=(
        "Return the selected conversation messages that the user chose to commit. "
        "Call this before commit_proposal_create so you can see the message contents "
        "and propose appropriate knowledge entries."
    ))
    @tool_visibility(ToolVisibility.LOW)
    async def commit_show_selected_messages(runtime: ToolRuntime) -> Command:
        commit_state = runtime.state.get("commit_proposal_state")
        payload = commit_state.get("payload") if commit_state else None
        if not payload:
            return Command(update={
                "messages": [ToolMessage(
                    content=json.dumps({"error": "No commit payload available."}),
                    tool_call_id=runtime.tool_call_id,
                )],
            })
        content = json.dumps({"messages": payload}, indent=2)
        return Command(update={
            "messages": [ToolMessage(content=content, tool_call_id=runtime.tool_call_id)],
        })

    @tool("commit_invoke_subagent", description=(
        "Send selected conversation messages to the commit subagent for knowledge extraction. "
        "The subagent will analyze the messages and propose structured knowledge entries. "
        "Use this for larger or more complex selections that benefit from dedicated processing. "
        "Pass 'context' to include relevant parent conversation context (e.g. the current topic). "
        "Pass 'conversation_id' from a previous response to continue refining the proposal. "
        "When revising after user edits, the current proposal state (with user changes) and "
        "the user's diff are automatically passed to the subagent."
    ))
    @tool_visibility(ToolVisibility.LOW)
    async def commit_invoke_subagent(
        runtime: ToolRuntime,
        instructions: str | None = None,
        context: str | None = None,
        conversation_id: str | None = None,
    ) -> Command:
        commit_state = runtime.state.get("commit_proposal_state")
        input_parts = []

        if conversation_id is None:
            # Fresh invocation — include the commit payload
            payload = commit_state.get("payload") if commit_state else None
            if payload:
                lines = []
                for entry in payload:
                    parts = []
                    if entry.get("user_context"):
                        parts.append(f"[User prompt]\n{entry['user_context']}")
                    parts.append(f"[Message {entry['index']}]\n{entry['content']}")
                    lines.append("\n".join(parts))
                input_parts.append(
                    "Selected messages for knowledge extraction:\n\n"
                    + "\n\n---\n\n".join(lines)
                )

        # Include the user edit diff if available
        proposal_diff = commit_state.get("proposal_diff") if commit_state else None
        if proposal_diff:
            input_parts.append(f"User edit summary:\n{proposal_diff}")

        if context:
            input_parts.append(f"Additional context:\n{context}")

        if instructions:
            input_parts.append(f"Instructions:\n{instructions}")

        input_text = "\n\n".join(input_parts)

        # Pass the current proposal so the subagent can see/edit user changes
        current_proposal = commit_state.get("proposal") if commit_state else None

        conv_id, ai_message, result_state = await subagent.ainvoke(
            input_text,
            conversation_id=conversation_id,
            extra_state={"commit_proposal": current_proposal or []},
        )

        proposal = result_state.get("commit_proposal")

        if proposal:
            state_update = {"commit_proposal_state": CommitProposalState(
                payload=commit_state["payload"] if commit_state else [],
                proposal=proposal,
                proposal_diff=None,
            )}
            msg = (
                f"Commit proposal staged: {len(proposal)} entry/entries. "
                f"conversation_id={conv_id}. "
                f"Call commit_proposal_present to show it to the user."
            )
        else:
            state_update = {}
            msg = (
                f"Subagent responded but produced no proposal entries. "
                f"conversation_id={conv_id}. "
                f"Response: {ai_message.content}"
            )

        state_update["messages"] = [ToolMessage(content=msg, tool_call_id=runtime.tool_call_id)]
        return Command(update=state_update)

    @tool("commit_proposal_create", description=(
        "Directly propose knowledge entries for commit without invoking the commit subagent. "
        "Use this when the selected messages are short and simple enough that you can propose "
        "entries yourself. Call commit_show_selected_messages first to see the selected messages."
    ))
    @tool_visibility(ToolVisibility.LOW)
    async def commit_proposal_create(
        entries: list[KnowledgeEntryProposalSchema],
        runtime: ToolRuntime,
    ) -> Command:
        if not entries:
            return Command(update={
                "messages": [ToolMessage(
                    content=json.dumps({"error": "No entries provided."}),
                    tool_call_id=runtime.tool_call_id,
                )],
            })

        proposal_entries = [
            CommitProposalEntry(
                id=i, title=e.title, content=e.content,
                entry_type=e.entry_type, topic_id=e.topic_id,
            )
            for i, e in enumerate(entries)
        ]
        commit_state = runtime.state.get("commit_proposal_state")
        msg = f"Commit proposal staged: {len(proposal_entries)} entry/entries. Call commit_proposal_present to show it to the user."
        return Command(update={
            "commit_proposal_state": CommitProposalState(
                payload=commit_state["payload"] if commit_state else [],
                proposal=proposal_entries,
                proposal_diff=None,
            ),
            "messages": [ToolMessage(content=msg, tool_call_id=runtime.tool_call_id)],
        })

    @tool("commit_proposal_present", description=(
        "Display the current commit proposal to the user for review. "
        "The user can approve, request edits, reset, or cancel. "
        "If edits requested, use commit_proposal_edit to make targeted "
        "changes (preserving any direct edits the user made), then present again. "
        "If the proposal was originally created by the subagent, you can also "
        "call commit_invoke_subagent with the conversation_id and instructions "
        "to have the subagent revise it."
    ))
    @tool_visibility(ToolVisibility.LOW)
    async def commit_proposal_present(runtime: ToolRuntime) -> Command:
        commit_state = runtime.state.get("commit_proposal_state")
        proposal = commit_state.get("proposal") if commit_state else None
        if not proposal:
            return Command(update={
                "messages": [ToolMessage(
                    content=json.dumps({"error": "No proposal available. Create or invoke a proposal first."}),
                    tool_call_id=runtime.tool_call_id,
                )],
            })

        # Build topic name map for display
        topic_ids = {e["topic_id"] for e in proposal}
        topic_map: dict[int, str] = {}
        async with session_factory() as session:
            for tid in topic_ids:
                topic = await get_topic(session, tid)
                if topic is not None:
                    topic_map[tid] = topic.name

        entries = [dict(e) for e in proposal]

        result = interrupt({
            "type": "commit_proposal",
            "entries": entries,
            "topic_map": topic_map,
        })

        choice = result["choice"]
        modified_entries = result.get("entries", [])
        new_proposal = [CommitProposalEntry(**e) for e in modified_entries]

        # Build diff summary
        originals_by_id = {e["id"]: e for e in proposal}
        diff_parts = _build_commit_diff(proposal, modified_entries, originals_by_id)
        diff_text = "\n".join(diff_parts)

        if choice == "Approve":
            msg_lines = [
                f"User approved {len(new_proposal)} entry/entries.",
                *diff_parts,
                "Call commit_proposal_accept to write them to the database.",
            ]
            return Command(update={
                "commit_proposal_state": CommitProposalState(
                    payload=commit_state["payload"] if commit_state else [],
                    proposal=new_proposal,
                    proposal_diff=None,
                ),
                "messages": [ToolMessage(
                    content="\n".join(msg_lines),
                    tool_call_id=runtime.tool_call_id,
                )],
            })
        elif choice == "Edit":
            instructions = result.get("instructions", "")
            msg_lines = [
                f"User requested edits: {instructions}",
                *diff_parts,
                f"Proposal state updated ({len(new_proposal)} entry/entries remaining).",
                "Use commit_proposal_edit to make further changes, then "
                "commit_proposal_present to show the revised proposal. "
                "Alternatively, if the proposal was created by the subagent, "
                "call commit_invoke_subagent with the conversation_id and "
                "instructions to have the subagent revise it.",
            ]
            return Command(update={
                "commit_proposal_state": CommitProposalState(
                    payload=commit_state["payload"] if commit_state else [],
                    proposal=new_proposal,
                    proposal_diff=diff_text,
                ),
                "messages": [ToolMessage(
                    content="\n".join(msg_lines),
                    tool_call_id=runtime.tool_call_id,
                )],
            })
        else:
            return Command(update={
                "commit_proposal_state": None,
                "messages": [ToolMessage(
                    content="User cancelled the proposal.",
                    tool_call_id=runtime.tool_call_id,
                )],
            })

    @tool("commit_proposal_edit", description=(
        "Make targeted edits to the current commit proposal without overwriting it. "
        "Supports in-place edits (partial field updates by stable ID), deletions (by ID), "
        "and additions (new entries appended with auto-assigned IDs). "
        "Processing order: edits, then deletions, then additions. "
        "Call commit_proposal_present afterwards to show the revised proposal to the user."
    ))
    @tool_visibility(ToolVisibility.LOW)
    async def commit_proposal_edit(
        runtime: ToolRuntime,
        edits: list[CommitEntryEdit] | None = None,
        additions: list[KnowledgeEntryProposalSchema] | None = None,
        deletions: list[int] | None = None,
    ) -> Command:
        commit_state = runtime.state.get("commit_proposal_state")
        proposal = commit_state.get("proposal") if commit_state else None
        if not proposal:
            return Command(update={
                "messages": [ToolMessage(
                    content=json.dumps({"error": "No commit proposal to edit. Create one first."}),
                    tool_call_id=runtime.tool_call_id,
                )],
            })

        entries = [dict(e) for e in proposal]
        entries_by_id = {e["id"]: e for e in entries}
        changes: list[str] = []

        for edit in (edits or []):
            entry = entries_by_id.get(edit.id)
            if entry is None:
                continue
            if edit.title is not None:
                entry["title"] = edit.title
            if edit.content is not None:
                entry["content"] = edit.content
            if edit.entry_type is not None:
                entry["entry_type"] = edit.entry_type
            if edit.topic_id is not None:
                entry["topic_id"] = edit.topic_id
            changes.append(f"edited entry {edit.id}")

        delete_ids = set(deletions or [])
        for did in sorted(delete_ids):
            if did in entries_by_id:
                changes.append(f"deleted entry {did} ({entries_by_id[did]['title']!r})")
        entries = [e for e in entries if e["id"] not in delete_ids]

        next_id = max((e["id"] for e in proposal), default=-1) + 1
        for addition in (additions or []):
            entries.append(CommitProposalEntry(
                id=next_id, title=addition.title, content=addition.content,
                entry_type=addition.entry_type, topic_id=addition.topic_id,
            ))
            changes.append(f"added entry {next_id} ({addition.title!r})")
            next_id += 1

        new_proposal = [CommitProposalEntry(**e) for e in entries]
        summary = "; ".join(changes) if changes else "no changes applied"
        msg = f"Commit proposal updated ({len(new_proposal)} entry/entries): {summary}."
        return Command(update={
            "commit_proposal_state": CommitProposalState(
                payload=commit_state["payload"] if commit_state else [],
                proposal=new_proposal,
                proposal_diff=commit_state.get("proposal_diff") if commit_state else None,
            ),
            "messages": [ToolMessage(content=msg, tool_call_id=runtime.tool_call_id)],
        })

    @tool("commit_proposal_accept", description=(
        "Write the accepted commit proposal to the database. "
        "Call this after the user has approved the proposal via commit_proposal_present."
    ))
    @tool_visibility(ToolVisibility.LOW)
    async def commit_proposal_accept(runtime: ToolRuntime) -> Command:
        commit_state = runtime.state.get("commit_proposal_state")
        proposal = commit_state.get("proposal") if commit_state else None
        if not proposal:
            return Command(update={
                "messages": [ToolMessage(
                    content=json.dumps({"error": "No proposal to accept."}),
                    tool_call_id=runtime.tool_call_id,
                )],
            })

        created = []
        async with session_factory() as session:
            for e in proposal:
                entry_type = EntryType(e["entry_type"]) if e.get("entry_type") else None
                entry = await create_entry(
                    session,
                    topic_id=e["topic_id"],
                    title=e["title"],
                    content=e["content"],
                    entry_type=entry_type,
                )
                created.append({"id": entry.id, "title": entry.title})
            await session.commit()

        if chat_pane is not None:
            chat_pane.post_message(CommitApproved(count=len(created)))

        msg = f"Committed {len(created)} knowledge entry/entries to the database."
        return Command(update={
            "commit_proposal_state": None,
            "messages": [ToolMessage(content=msg, tool_call_id=runtime.tool_call_id)],
        })

    return [
        commit_show_selected_messages,
        commit_invoke_subagent,
        commit_proposal_create,
        commit_proposal_present,
        commit_proposal_edit,
        commit_proposal_accept,
    ]
