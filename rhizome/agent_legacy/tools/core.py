"""Core tools — topics, entries, and flashcard lookup for the agent."""

from __future__ import annotations

from langchain.tools import tool
from langgraph.types import interrupt
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from rhizome.agent_legacy.tools.visibility import ToolVisibility, tool_visibility

from rhizome.db.models import KnowledgeEntry, Topic
from rhizome.db.operations import (
    create_topic,
    delete_topic,
    get_entry,
    get_flashcards_by_ids,
    get_subtree,
    get_topic,
    list_entries,
    list_flashcards_by_entries,
    list_flashcards_by_topic,
)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class TopicNode(BaseModel):
    """A node in a topic subtree to create."""
    name: str = Field(description="Name of the topic")
    description: str | None = Field(default=None, description="Optional description")
    children: list["TopicNode"] = Field(default_factory=list, description="Child topics to create under this one")


def build_core_tools(session_factory) -> dict:
    """Build core knowledge-base tools with session_factory closed over."""

    # -----------------------------------------------------------------------
    # Topics
    # -----------------------------------------------------------------------

    @tool_visibility(ToolVisibility.DEFAULT)
    @tool("list_topics", description=(
        "List the entire topic tree with entry counts. "
        "Returns a nested, indented view of all topics showing [id], name, "
        "and how many knowledge entries each topic contains."
    ))
    async def list_topics_tool() -> str:
        async with session_factory() as session:
            # Fetch all topics and entry counts in two queries
            all_topics = (await session.execute(
                select(Topic).order_by(Topic.id)
            )).scalars().all()

            counts = dict((await session.execute(
                select(KnowledgeEntry.topic_id, func.count())
                .group_by(KnowledgeEntry.topic_id)
            )).all())

        if not all_topics:
            return "No topics found."

        # Build tree structure
        by_parent: dict[int | None, list[Topic]] = {}
        for t in all_topics:
            by_parent.setdefault(t.parent_id, []).append(t)

        lines: list[str] = []
        def walk(parent_id: int | None, depth: int) -> None:
            for t in by_parent.get(parent_id, []):
                count = counts.get(t.id, 0)
                indent = "  " * depth
                lines.append(f"{indent}- [{t.id}] {t.name} ({count} entries)")
                walk(t.id, depth + 1)

        walk(None, 0)
        return "\n".join(lines)

    @tool_visibility(ToolVisibility.DEFAULT)
    @tool("list_knowledge_entries", description=(
        "Show one or more topics' details and list all their knowledge entries by title and ID. "
        "Use read_knowledge_entries to read the full content of specific entries."
    ))
    async def list_knowledge_entries_tool(topic_ids: list[int]) -> str:
        results: list[str] = []
        async with session_factory() as session:
            for topic_id in topic_ids:
                topic = await get_topic(session, topic_id)
                if topic is None:
                    results.append(f"Topic {topic_id} not found.")
                    continue
                entries = await list_entries(session, topic_id)

                lines = [f"Topic [{topic.id}]: {topic.name}"]
                if topic.description:
                    lines.append(f"Description: {topic.description}")
                lines.append("")
                if not entries:
                    lines.append("No entries in this topic.")
                else:
                    lines.append(f"{len(entries)} entries:")
                    for e in entries:
                        type_str = f" ({e.entry_type.value})" if e.entry_type else ""
                        lines.append(f"  - [{e.id}] {e.title}{type_str}")
                results.append("\n".join(lines))
        return "\n\n---\n\n".join(results)

    @tool_visibility(ToolVisibility.DEFAULT)
    @tool("create_topics", description=(
        "Create one or more topics, optionally as subtrees. Each TopicNode has a "
        "name, optional description, and optional children (which nest recursively). "
        "Use parent_id to attach the new topics under an existing topic."
    ))
    async def create_topics_tool(
        topics: list[TopicNode],
        parent_id: int | None = None,
    ) -> str:
        created: list[str] = []

        async def _create_recursive(
            session, nodes: list[TopicNode], pid: int | None,
        ) -> None:
            for node in nodes:
                topic = await create_topic(
                    session, name=node.name, parent_id=pid,
                    description=node.description,
                )
                created.append(f"[{topic.id}] {topic.name}")
                if node.children:
                    await _create_recursive(session, node.children, topic.id)

        async with session_factory() as session:
            await _create_recursive(session, topics, parent_id)
            await session.commit()

        return f"Created {len(created)} topic(s):\n" + "\n".join(f"  - {c}" for c in created)

    @tool_visibility(ToolVisibility.DEFAULT)
    @tool("delete_topics", description=(
        "Delete one or more topics by ID. This is irreversible — all knowledge "
        "entries under each topic will also be deleted. Subtrees (child topics) "
        "are deleted bottom-up automatically. Requires user approval."
    ))
    async def delete_topics_tool(topic_ids: list[int]) -> str:
        # Gather info for the warning message
        topic_names: list[str] = []
        async with session_factory() as session:
            for tid in topic_ids:
                topic = await get_topic(session, tid)
                if topic is None:
                    return f"Topic {tid} not found."
                subtree = await get_subtree(session, tid)
                entry_count = (await session.execute(
                    select(func.count()).where(KnowledgeEntry.topic_id == tid)
                )).scalar() or 0
                child_count = len(subtree)
                parts = [f"[{tid}] {topic.name}"]
                if child_count:
                    parts.append(f"{child_count} subtopic(s)")
                if entry_count:
                    parts.append(f"{entry_count} entry/entries")
                topic_names.append(", ".join(parts))

        summary = "; ".join(topic_names)
        result = interrupt({
            "type": "warning",
            "message": (
                f"WARNING: the agent has requested to delete topic(s): "
                f"{summary}. This action is irreversible and will cascade to "
                f"all entries and subtopics."
            ),
        })

        if result != "Approve":
            return f"User denied deletion: {result}"

        # Perform deletion — subtrees must be deleted bottom-up
        deleted: list[str] = []
        async with session_factory() as session:
            for tid in topic_ids:
                topic = await get_topic(session, tid)
                if topic is None:
                    deleted.append(f"[{tid}] not found (skipped)")
                    continue
                # Delete subtree bottom-up (deepest first)
                subtree = await get_subtree(session, tid)
                for node in reversed(subtree):
                    await delete_topic(session, node["topic"].id)
                # Delete the root topic itself
                name = topic.name
                await delete_topic(session, tid)
                deleted.append(f"[{tid}] {name}")
            await session.commit()
        return f"Deleted {len(deleted)} topic(s):\n" + "\n".join(f"  - {d}" for d in deleted)

    # -----------------------------------------------------------------------
    # Entries
    # -----------------------------------------------------------------------

    @tool_visibility(ToolVisibility.DEFAULT)
    @tool("read_knowledge_entries", description=(
        "Get the full details of one or more knowledge entries by their IDs."
    ))
    async def read_knowledge_entries_tool(knowledge_entry_ids: list[int]) -> str:
        results: list[str] = []
        async with session_factory() as session:
            for eid in knowledge_entry_ids:
                entry = await get_entry(session, eid)
                if entry is None:
                    results.append(f"[{eid}] Not found.")
                    continue
                lines = [
                    f"[{entry.id}] {entry.title}",
                    f"Type: {entry.entry_type.value if entry.entry_type else 'unset'}",
                    f"Content: {entry.content}",
                ]
                if entry.additional_notes:
                    lines.append(f"Notes: {entry.additional_notes}")
                if entry.difficulty is not None:
                    lines.append(f"Difficulty: {entry.difficulty}")
                results.append("\n".join(lines))
        return "\n\n---\n\n".join(results)

    # -----------------------------------------------------------------------
    # Flashcards
    # -----------------------------------------------------------------------

    @tool_visibility(ToolVisibility.DEFAULT)
    @tool("list_flashcards", description=(
        "List existing flashcards by topic or by knowledge entry. "
        "Provide exactly one of topic_ids or knowledge_entry_ids. "
        "When querying by knowledge_entry_ids, excludes flashcards from ephemeral sessions. "
        "Returns a summary of flashcard IDs and coverage."
    ))
    async def list_flashcards_tool(
        topic_ids: list[int] | None = None,
        knowledge_entry_ids: list[int] | None = None,
    ) -> str:
        if (topic_ids is None) == (knowledge_entry_ids is None):
            return "Error: provide exactly one of topic_ids or knowledge_entry_ids."

        if topic_ids is not None:
            all_flashcards = []
            async with session_factory() as session:
                for tid in topic_ids:
                    fcs = await list_flashcards_by_topic(session, tid)
                    all_flashcards.extend(fcs)

            if not all_flashcards:
                return f"No flashcards found for {len(topic_ids)} topic(s)."

            # Deduplicate by ID
            seen: set[int] = set()
            unique: list = []
            for fc in all_flashcards:
                if fc.id not in seen:
                    seen.add(fc.id)
                    unique.append(fc)

            lines = [f"Found {len(unique)} flashcard(s) across {len(topic_ids)} topic(s):"]
            for fc in unique:
                lines.append(f"  - [{fc.id}] Q: {fc.question_text[:60]}...")
            return "\n".join(lines)

        # knowledge_entry_ids path
        entry_ids = knowledge_entry_ids
        async with session_factory() as session:
            flashcards = await list_flashcards_by_entries(session, entry_ids)

        if not flashcards:
            return f"No existing flashcards found for {len(entry_ids)} entries."

        # Group flashcards by entry
        entry_to_flashcards: dict[int, list[int]] = {}
        for fc in flashcards:
            for fe in fc.flashcard_entries:
                entry_to_flashcards.setdefault(fe.entry_id, []).append(fc.id)

        covered = set(entry_to_flashcards.keys()) & set(entry_ids)
        uncovered = set(entry_ids) - covered

        lines = [f"Found {len(flashcards)} flashcard(s) across {len(covered)} entries:"]
        for eid in sorted(covered):
            fc_ids = entry_to_flashcards[eid]
            lines.append(f"  Entry [{eid}]: {len(fc_ids)} flashcard(s) (IDs: {', '.join(str(i) for i in fc_ids)})")

        if uncovered:
            lines.append(f"\n{len(uncovered)} entries have no flashcards: {sorted(uncovered)}")

        return "\n".join(lines)

    @tool_visibility(ToolVisibility.DEFAULT)
    @tool("read_flashcards", description=(
        "Get full flashcard content by IDs: question_text, answer_text, "
        "testing_notes, and linked entry_ids. This does NOT present the cards "
        "to the user, only as an internal tool message for the agent."
    ))
    async def read_flashcards_tool(flashcard_ids: list[int]) -> str:
        async with session_factory() as session:
            flashcards = await get_flashcards_by_ids(session, flashcard_ids)

        if not flashcards:
            return "No flashcards found for the given IDs."

        lines: list[str] = []
        for fc in flashcards:
            entry_ids = [fe.entry_id for fe in fc.flashcard_entries]
            parts = [
                f"Flashcard [{fc.id}]",
                f"Topic: {fc.topic_id}",
                f"Q: {fc.question_text}",
                f"A: {fc.answer_text}",
            ]
            if fc.testing_notes:
                parts.append(f"Testing notes: {fc.testing_notes}")
            parts.append(f"Entries: {entry_ids}")
            lines.append("\n".join(parts))

        return "\n\n---\n\n".join(lines)

    return {
        "list_topics": list_topics_tool,
        "list_knowledge_entries": list_knowledge_entries_tool,
        "create_topics": create_topics_tool,
        "delete_topics": delete_topics_tool,
        "read_knowledge_entries": read_knowledge_entries_tool,
        "list_flashcards": list_flashcards_tool,
        "read_flashcards": read_flashcards_tool,
    }
