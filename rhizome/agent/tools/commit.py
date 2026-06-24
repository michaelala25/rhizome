"""Commit workflow tools — turn selected learn-mode messages into knowledge entries.

These are the root agent's handles on the commit workflow: inspect the selected messages, propose entries
(directly or by delegating to the commit subagent), present the proposal to the user, edit it, and write the
approved entries to the DB. Proposal + payload state lives in ``RootAgentState.commit_proposal_state`` (a
``CommitProposalState``: the selected-message payload, the staged entries, and the most recent user-edit
diff).

``commit_invoke_subagent`` delegates extraction to a dedicated subagent reached through the live
``AgentRuntime`` on the context, under the key ``commit``. The subagent owns one conversation per
proposal; this tool emits that conversation's ``thread_id`` so a later call can resume it
(``ctx.runtime.get("commit", thread_id)``) to refine the same proposal. The subagent's contract: it stages
its result in its own state field ``commit_proposal``, which this tool reads back from the run's final
state. The subagent kind itself is registered elsewhere (the subagent factory).
"""

from __future__ import annotations

import json

from langchain.tools import tool
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from rhizome.db.models import EntryType
from rhizome.db.operations import create_entry, get_topic
from rhizome.logs import get_logger

from ..base import MessagePayload, StateUpdatePayload
from ..state import CommitProposalEntry, CommitProposalState
from .visibility import ToolVisibility, tool_visibility

_logger = get_logger("agent.commit_tools")

# Runtime key for the knowledge-extraction subagent (registered by the subagent factory).
COMMIT_SUBAGENT_KEY = "commit"


# ---------------------------------------------------------------------------
# Input schemas
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
    entry_type: str | None = Field(
        default=None, description="New entry type: fact, exposition, or overview (omit to keep current)"
    )
    topic_id: int | None = Field(default=None, description="New topic ID (omit to keep current)")


# ---------------------------------------------------------------------------
# Diff helper
# ---------------------------------------------------------------------------

def _build_commit_diff(
    original: list[dict],
    returned: list[dict],
    originals_by_id: dict[int, dict],
) -> list[str]:
    """Compare original proposal entries against widget-returned entries. Returns human-readable lines
    describing exclusions and edits."""
    returned_ids = {e["id"] for e in returned}
    original_ids = {e["id"] for e in original}

    parts: list[str] = []

    excluded_ids = sorted(original_ids - returned_ids)
    if excluded_ids:
        labels = [f"entry {eid} ({originals_by_id[eid]['title']!r})" for eid in excluded_ids]
        parts.append(f"Excluded by user: {', '.join(labels)}")

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
# Tool builder
# ---------------------------------------------------------------------------

def build_commit_tools() -> dict:
    """Build the commit-workflow tools (name -> tool). These are root-agent tools: they pull their DB
    session factory and the ``AgentRuntime`` (for the commit subagent) off the agent context at call time,
    rather than closing over them."""

    @tool_visibility(ToolVisibility.LOW)
    @tool("commit_show_selected_messages", description=(
        "Return the selected conversation messages that the user chose to commit. "
        "Call this before commit_proposal_create so you can see the message contents "
        "and propose appropriate knowledge entries."
    ))
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

    @tool_visibility(ToolVisibility.LOW)
    @tool("commit_invoke_subagent", description=(
        "Send selected conversation messages to the commit subagent for knowledge extraction. "
        "The subagent analyzes the messages and proposes structured knowledge entries. "
        "Use this for larger or more complex selections that benefit from dedicated processing. "
        "Pass 'context' to include relevant parent conversation context (e.g. the current topic). "
        "Pass 'thread_id' from a previous response to continue refining the same proposal. "
        "When revising after user edits, the user's diff is passed to the subagent automatically."
    ))
    async def commit_invoke_subagent(
        runtime: ToolRuntime,
        instructions: str | None = None,
        context: str | None = None,
        thread_id: str | None = None,
    ) -> Command:
        agent_runtime = getattr(runtime.context, "runtime", None)
        if agent_runtime is None:
            return Command(update={
                "messages": [ToolMessage(
                    content="The commit subagent is unavailable in this conversation.",
                    tool_call_id=runtime.tool_call_id,
                )],
            })

        commit_state = runtime.state.get("commit_proposal_state")
        input_parts: list[str] = []

        if thread_id is None:
            # Fresh invocation — include the commit payload (the selected messages).
            payload = commit_state.get("payload") if commit_state else None
            if payload:
                lines = []
                for entry in payload:
                    entry_parts = []
                    if entry.get("user_context"):
                        entry_parts.append(f"[User prompt]\n{entry['user_context']}")
                    entry_parts.append(f"[Message {entry['index']}]\n{entry['content']}")
                    lines.append("\n".join(entry_parts))
                input_parts.append(
                    "Selected messages for knowledge extraction:\n\n" + "\n\n---\n\n".join(lines)
                )

        # Include the user edit diff if available.
        proposal_diff = commit_state.get("proposal_diff") if commit_state else None
        if proposal_diff:
            input_parts.append(f"User edit summary:\n{proposal_diff}")
        if context:
            input_parts.append(f"Additional context:\n{context}")
        if instructions:
            input_parts.append(f"Instructions:\n{instructions}")

        input_text = "\n\n".join(input_parts)
        current_proposal = commit_state.get("proposal") if commit_state else None

        # Fresh thread vs. resume — a resumed thread carries its own ``commit_proposal`` forward.
        try:
            subagent = (agent_runtime.get(COMMIT_SUBAGENT_KEY, thread_id) if thread_id is not None
                        else agent_runtime.new(COMMIT_SUBAGENT_KEY))
        except KeyError:
            return Command(update={
                "messages": [ToolMessage(
                    content="The commit subagent is not available (no such conversation or agent kind).",
                    tool_call_id=runtime.tool_call_id,
                )],
            })

        # Feed the subagent through its own input queue, the same way any session takes input: a
        # StateUpdatePayload carries the current (possibly user-edited) proposal into its ``commit_proposal``
        # state — merged through the subagent's reducers at the next compile, so a revision builds on that
        # version — and the message carries the extraction request. Sent on every call (fresh or resumed),
        # so the subagent always revises the proposal the user is actually looking at.
        payloads: list = []
        if current_proposal:
            payloads.append(StateUpdatePayload(data={"commit_proposal": current_proposal}))
        payloads.append(MessagePayload(data=input_text, role=MessagePayload.Role.USER))

        result = await subagent.invoke(payloads)
        proposal = result.state.get("commit_proposal")
        conv_thread = result.thread_id

        if proposal:
            state_update: dict = {"commit_proposal_state": CommitProposalState(
                payload=commit_state["payload"] if commit_state else [],
                proposal=proposal,
                proposal_diff=None,
            )}
            msg = (
                f"Commit proposal staged: {len(proposal)} entry/entries. thread_id={conv_thread}. "
                f"Call commit_proposal_present to show it to the user."
            )
        else:
            state_update = {}
            response_text = result.response.content if result.response else "(no response)"
            msg = (
                f"Subagent responded but produced no proposal entries. thread_id={conv_thread}. "
                f"Response: {response_text}"
            )

        state_update["messages"] = [ToolMessage(content=msg, tool_call_id=runtime.tool_call_id)]
        return Command(update=state_update)

    @tool_visibility(ToolVisibility.LOW)
    @tool("commit_proposal_create", description=(
        "Directly propose knowledge entries for commit without invoking the commit subagent. "
        "Use this when the selected messages are short and simple enough that you can propose "
        "entries yourself. Call commit_show_selected_messages first to see the selected messages."
    ))
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
                id=i, title=e.title, content=e.content, entry_type=e.entry_type, topic_id=e.topic_id,
            )
            for i, e in enumerate(entries)
        ]
        commit_state = runtime.state.get("commit_proposal_state")
        msg = (
            f"Commit proposal staged: {len(proposal_entries)} entry/entries. "
            f"Call commit_proposal_present to show it to the user."
        )
        return Command(update={
            "commit_proposal_state": CommitProposalState(
                payload=commit_state["payload"] if commit_state else [],
                proposal=proposal_entries,
                proposal_diff=None,
            ),
            "messages": [ToolMessage(content=msg, tool_call_id=runtime.tool_call_id)],
        })

    @tool_visibility(ToolVisibility.LOW)
    @tool("commit_proposal_present", description=(
        "Display the current commit proposal to the user for review. "
        "The user can approve, request edits, reset, or cancel. "
        "If edits requested, use commit_proposal_edit to make targeted "
        "changes (preserving any direct edits the user made), then present again. "
        "If the proposal was originally created by the subagent, you can also "
        "call commit_invoke_subagent with the thread_id and instructions "
        "to have the subagent revise it."
    ))
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
        async with runtime.context.session_factory() as session:
            for tid in topic_ids:
                topic = await get_topic(session, tid)
                if topic is not None:
                    topic_map[tid] = topic.name

        entries = [dict(e) for e in proposal]

        result = interrupt({"type": "commit_proposal", "entries": entries, "topic_map": topic_map})

        # The proposal surface resolves into {"accepted": [...] | None, "edit_instructions": str}: cancel
        # is ``accepted is None``; approve vs. revise is whether ``edit_instructions`` is set. ``accepted``
        # carries the kept entries (the user's edits + exclusions applied) back in the proposal dict shape.
        accepted = result.get("accepted") if isinstance(result, dict) else None
        if accepted is None:
            return Command(update={
                "commit_proposal_state": None,
                "messages": [ToolMessage(
                    content="User cancelled the proposal.",
                    tool_call_id=runtime.tool_call_id,
                )],
            })

        edit_instructions = (result.get("edit_instructions") or "").strip()
        new_proposal = [CommitProposalEntry(**e) for e in accepted]
        originals_by_id = {e["id"]: e for e in proposal}
        diff_parts = _build_commit_diff(proposal, accepted, originals_by_id)
        diff_text = "\n".join(diff_parts)

        if edit_instructions:
            msg_lines = [
                f"User requested edits: {edit_instructions}",
                *diff_parts,
                f"Proposal state updated ({len(new_proposal)} entry/entries remaining).",
                "Use commit_proposal_edit to make further changes, then "
                "commit_proposal_present to show the revised proposal. "
                "Alternatively, if the proposal was created by the subagent, "
                "call commit_invoke_subagent with the thread_id and instructions "
                "to have the subagent revise it.",
            ]
            return Command(update={
                "commit_proposal_state": CommitProposalState(
                    payload=commit_state["payload"] if commit_state else [],
                    proposal=new_proposal,
                    proposal_diff=diff_text,
                ),
                "messages": [ToolMessage(content="\n".join(msg_lines), tool_call_id=runtime.tool_call_id)],
            })

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
            "messages": [ToolMessage(content="\n".join(msg_lines), tool_call_id=runtime.tool_call_id)],
        })

    @tool_visibility(ToolVisibility.LOW)
    @tool("commit_proposal_edit", description=(
        "Make targeted edits to the current commit proposal without overwriting it. "
        "Supports in-place edits (partial field updates by stable ID), deletions (by ID), "
        "and additions (new entries appended with auto-assigned IDs). "
        "Processing order: edits, then deletions, then additions. "
        "Call commit_proposal_present afterwards to show the revised proposal to the user."
    ))
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

    @tool_visibility(ToolVisibility.LOW)
    @tool("commit_proposal_accept", description=(
        "Write the accepted commit proposal to the database. "
        "Call this after the user has approved the proposal via commit_proposal_present."
    ))
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
        async with runtime.context.session_factory() as session:
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

        msg = f"Committed {len(created)} knowledge entry/entries to the database."
        return Command(update={
            "commit_proposal_state": None,
            "messages": [ToolMessage(content=msg, tool_call_id=runtime.tool_call_id)],
        })

    return {
        "commit_show_selected_messages": commit_show_selected_messages,
        "commit_invoke_subagent": commit_invoke_subagent,
        "commit_proposal_create": commit_proposal_create,
        "commit_proposal_present": commit_proposal_present,
        "commit_proposal_edit": commit_proposal_edit,
        "commit_proposal_accept": commit_proposal_accept,
    }
