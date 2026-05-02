"""Review-mode tools for review sessions.

Each tool creates its own DB session via a closure over ``session_factory``,
matching the pattern in other tool modules.  Tools that mutate ReviewState
return ``Command(update={"review": ...})``.
"""

from __future__ import annotations

from typing import Literal

from langchain.tools import tool
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from rhizome.agent.state import ReviewConfig, ReviewScope, ReviewState
from rhizome.agent.tools.visibility import ToolVisibility, tool_visibility
from rhizome.db.models import (
    KnowledgeEntry,
    ReviewSessionEntry,
    ReviewSessionTopic,
)
from rhizome.db.operations import (
    add_review_interaction,
    commit_fsrs_card,
    complete_review_session,
    create_review_session,
    get_flashcard_entry_ids,
    get_flashcards_by_ids,
    get_interaction_stats,
    get_sessions_by_topics,
    to_fsrs_card,
    update_session_ephemeral,
    update_session_instructions,
    update_session_plan,
    update_session_summary,
)
from rhizome.logs import get_logger

_logger = get_logger("agent.review_tools")



# ---------------------------------------------------------------------------
# Pydantic schemas for review_update_session_state
# ---------------------------------------------------------------------------

class ReviewConfigUpdate(BaseModel):
    """Partial update to review configuration.  Only provided fields are applied."""
    style: str | None = Field(default=None, description="Review style: 'flashcard', 'conversation', or 'mixed'")
    critique_timing: str | None = Field(default=None, description="When to deliver critique: 'during' or 'after'")
    ephemeral: bool | None = Field(default=None, description="If true, session is not persisted for long-term tracking")
    user_instructions: str | None = Field(default=None, description="Free-form instructions from the user")


class ReviewFlashcardUpdate(BaseModel):
    """Update to the flashcard queue."""
    action: Literal["append", "set", "remove", "clear"] = Field(
        default="append",
        description="'append' adds to queue, 'set' replaces queue, 'remove' removes specific IDs, 'clear' empties queue",
    )
    flashcard_ids: list[int] = Field(default_factory=list, description="Flashcard IDs (ignored for 'clear')")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _empty_review_state(session_id: int) -> ReviewState:
    """Create a blank ReviewState with the given DB session ID."""
    return ReviewState(
        session_id=session_id,
        scope=None,
        config=None,
        flashcard_queue=[],
        entry_coverage={},
        interaction_count=0,
        discussion_plan=None,
    )


_NOT_INITIALIZED_MSG = (
    "Error: no active review session. Call review_start_session first."
)


def _require_review_state(runtime: ToolRuntime) -> ReviewState | None:
    """Return the active ReviewState, or None if no session has been started."""
    return runtime.state.get("review")


def _not_initialized_command(tool_call_id: str) -> Command:
    """Build the error Command for tools called before review_start_session."""
    return Command(update={
        "messages": [ToolMessage(
            content=_NOT_INITIALIZED_MSG,
            tool_call_id=tool_call_id,
        )],
    })


async def _update_scope_in_db(
    session_factory,
    session_id: int,
    topic_ids: list[int],
    entry_ids: list[int],
) -> None:
    """Replace the scope junction rows for a review session."""
    async with session_factory() as session:
        # Clear existing
        await session.execute(
            delete(ReviewSessionTopic).where(ReviewSessionTopic.session_id == session_id)
        )
        await session.execute(
            delete(ReviewSessionEntry).where(ReviewSessionEntry.session_id == session_id)
        )
        # Insert new
        for tid in topic_ids:
            session.add(ReviewSessionTopic(session_id=session_id, topic_id=tid))
        for eid in entry_ids:
            session.add(ReviewSessionEntry(session_id=session_id, entry_id=eid))
        await session.commit()


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------

def build_review_tools(session_factory) -> dict:
    """Build all review-mode tool functions with session_factory closed over.

    Flashcard auto-scoring is handled inside ``FlashcardReview`` (the TUI
    widget), so the scorer subagent is no longer constructed or passed in
    here."""

    # -------------------------------------------------------------------
    # review_get_past_sessions
    # -------------------------------------------------------------------

    @tool_visibility(ToolVisibility.LOW)
    @tool("review_get_past_sessions", description=(
        "Get past review sessions overlapping the given topic IDs. "
        "Returns session date, scope summary, and final_summary text. "
        "Excludes ephemeral sessions. Ranked by topic overlap (IoU), limited to 5."
    ))
    async def review_get_past_sessions_tool(topic_ids: list[int]) -> str:
        async with session_factory() as session:
            sessions = await get_sessions_by_topics(session, topic_ids)

        if not sessions:
            return "No prior review sessions found for these topics."

        lines: list[str] = []
        for rs in sessions:
            parts = [f"Session #{rs.id}"]
            parts.append(f"Date: {rs.created_at.strftime('%Y-%m-%d %H:%M')}")
            if rs.completed_at:
                parts.append("Status: completed")
            else:
                parts.append("Status: incomplete")
            if rs.final_summary:
                parts.append(f"Summary:\n{rs.final_summary}")
            else:
                parts.append("Summary: (none)")
            lines.append("\n".join(parts))

        return "\n\n---\n\n".join(lines)

    # -------------------------------------------------------------------
    # review_show_session_state
    # -------------------------------------------------------------------

    @tool("review_show_session_state", description=(
        "Dump the current review session state as a readable summary. "
        "Shows session ID, scope, config, queue size, coverage, and interaction count."
    ))
    @tool_visibility(ToolVisibility.LOW)
    async def review_show_session_state_tool(runtime: ToolRuntime) -> str:
        review_state: ReviewState | None = runtime.state.get("review")
        if review_state is None:
            return "No active review session."

        total_entries = len(review_state["entry_coverage"])
        touched = sum(1 for c in review_state["entry_coverage"].values() if c > 0)

        lines = [
            f"Session ID: {review_state['session_id']}",
        ]

        scope = review_state.get("scope")
        if scope:
            lines.append(f"Scope: {len(scope['entry_ids'])} entries across {len(scope['topic_ids'])} topics")
        else:
            lines.append("Scope: (not set)")

        config = review_state.get("config")
        if config:
            # Config is built up incrementally via review_update_session_state,
            # so any subset of keys may be present. Render only what's set.
            shown = {
                "style": config.get("style"),
                "timing": config.get("critique_timing"),
                "ephemeral": config.get("ephemeral"),
            }
            parts = [f"{k}={v}" for k, v in shown.items() if v is not None]
            lines.append(
                f"Config: {', '.join(parts)}" if parts else "Config: (set, no fields)"
            )
        else:
            lines.append("Config: (not set)")

        lines.append(f"Flashcard queue: {len(review_state['flashcard_queue'])} remaining")
        lines.append(f"Coverage: {touched}/{total_entries} entries touched")
        lines.append(f"Interactions: {review_state['interaction_count']}")

        if review_state.get("discussion_plan"):
            lines.append("Plan: (set)")

        return "\n".join(lines)

    # -------------------------------------------------------------------
    # review_start_session
    # -------------------------------------------------------------------

    @tool_visibility(ToolVisibility.LOW)
    @tool("review_start_session", description=(
        "Start a new review session. Creates the underlying DB record and "
        "initializes the in-memory review state. MUST be called before any "
        "other review_* tool that mutates session state "
        "(review_update_session_state, review_record_interaction, "
        "review_present_flashcards, review_finish_session). "
        "If a session is already active, this tool returns an error — call "
        "review_finish_session or review_update_session_state(clear=True) first."
    ))
    async def review_start_session_tool(runtime: ToolRuntime) -> Command:
        if runtime.state.get("review") is not None:
            return Command(update={
                "messages": [ToolMessage(
                    content=(
                        "Error: a review session is already active. "
                        "Finish or clear it before starting a new one."
                    ),
                    tool_call_id=runtime.tool_call_id,
                )],
            })

        async with session_factory() as session:
            review_session = await create_review_session(
                session, topic_ids=[], entry_ids=[],
            )
            await session.commit()
            session_id = review_session.id

        new_state = _empty_review_state(session_id)
        msg = f"Review session started (DB session #{session_id})."
        return Command(update={
            "review": new_state,
            "messages": [ToolMessage(content=msg, tool_call_id=runtime.tool_call_id)],
        })

    # -------------------------------------------------------------------
    # review_update_session_state
    # -------------------------------------------------------------------

    @tool_visibility(ToolVisibility.LOW)
    @tool("review_update_session_state", description=(
        "Update the active review session state. Requires that "
        "review_start_session has been called first. "
        "All parameters are optional — only provided values are applied.\n\n"
        "- scope: list of entry_ids to set as the review scope (derives topic_ids automatically).\n"
        "- config_update: partial config update (style, critique_timing, ephemeral, user_instructions).\n"
        "- flashcards: update the flashcard queue (append/set/remove/clear).\n"
        "- plan: set the discussion plan for conversational review.\n"
        "- clear: abandon the session and clear all state (DB records remain)."
    ))
    async def review_update_session_state_tool(
        runtime: ToolRuntime,
        scope: list[int] | None = None,
        # NB: parameter must NOT be named ``config`` — ``StructuredTool._arun``
        # has a keyword-only ``config: RunnableConfig`` slot that silently
        # absorbs any kwarg of that name, so the tool body would receive None.
        config_update: ReviewConfigUpdate | None = None,
        flashcards: ReviewFlashcardUpdate | None = None,
        plan: str | None = None,
        clear: bool = False,
    ) -> Command:
        # -- Clear --
        if clear:
            msg = "Review state cleared."
            return Command(update={
                "review": None,
                "messages": [ToolMessage(content=msg, tool_call_id=runtime.tool_call_id)],
            })

        review_state = _require_review_state(runtime)
        if review_state is None:
            return _not_initialized_command(runtime.tool_call_id)

        session_id = review_state["session_id"]
        # Returned as the right-hand side of merge_review_state — must be a
        # PARTIAL dict containing only keys this call modified, so concurrent
        # updates touching other keys aren't clobbered.
        partial: dict = {}
        results: list[str] = []

        # -- Scope --
        if scope is not None:
            entry_ids = list(scope)
            async with session_factory() as session:
                result = await session.execute(
                    select(KnowledgeEntry.topic_id)
                    .where(KnowledgeEntry.id.in_(entry_ids))
                    .distinct()
                )
                topic_ids = list(result.scalars().all())

            await _update_scope_in_db(session_factory, session_id, topic_ids, entry_ids)

            existing_coverage = review_state["entry_coverage"]
            partial["scope"] = ReviewScope(topic_ids=topic_ids, entry_ids=entry_ids)
            partial["entry_coverage"] = {eid: existing_coverage.get(eid, 0) for eid in entry_ids}
            results.append(f"Scope set: {len(entry_ids)} entries across {len(topic_ids)} topics.")

        # -- Config --
        if config_update is not None:
            existing_config = review_state.get("config") or {}
            updated = dict(existing_config)

            if config_update.style is not None:
                updated["style"] = config_update.style
            if config_update.critique_timing is not None:
                updated["critique_timing"] = config_update.critique_timing
            if config_update.ephemeral is not None:
                updated["ephemeral"] = config_update.ephemeral
                async with session_factory() as session:
                    await update_session_ephemeral(session, session_id, config_update.ephemeral)
                    await session.commit()
            if config_update.user_instructions is not None:
                updated["user_instructions"] = config_update.user_instructions
                async with session_factory() as session:
                    await update_session_instructions(session, session_id, config_update.user_instructions)
                    await session.commit()

            partial["config"] = ReviewConfig(**updated) if updated else None

            set_fields = [k for k, v in (config_update.model_dump()).items() if v is not None]
            results.append(f"Config updated: {', '.join(set_fields)}.")

        # -- Flashcards --
        if flashcards is not None:
            queue = list(review_state["flashcard_queue"])
            action = flashcards.action
            ids = flashcards.flashcard_ids

            if action == "append":
                queue.extend(ids)
                results.append(f"Appended {len(ids)} flashcard(s) to queue. Queue size: {len(queue)}.")
            elif action == "set":
                queue = list(ids)
                results.append(f"Flashcard queue set: {len(queue)} card(s).")
            elif action == "remove":
                remove_set = set(ids)
                queue = [fid for fid in queue if fid not in remove_set]
                results.append(f"Removed {len(ids)} flashcard(s) from queue. Queue size: {len(queue)}.")
            elif action == "clear":
                queue = []
                results.append("Flashcard queue cleared.")

            partial["flashcard_queue"] = queue

        # -- Plan --
        if plan is not None:
            async with session_factory() as session:
                await update_session_plan(session, session_id, plan)
                await session.commit()
            partial["discussion_plan"] = plan
            results.append("Discussion plan set.")

        if not results:
            results.append("No updates applied.")

        msg = " ".join(results)
        update: dict = {
            "messages": [ToolMessage(content=msg, tool_call_id=runtime.tool_call_id)],
        }
        if partial:
            update["review"] = partial
        return Command(update=update)

    # -------------------------------------------------------------------
    # review_record_interaction
    # -------------------------------------------------------------------

    @tool_visibility(ToolVisibility.LOW)
    @tool("review_record_interaction", description=(
        "Record a conversational review checkpoint. "
        "For flashcard-based interactions use review_present_flashcards instead. "
        "Updates entry coverage and interaction count."
    ))
    async def review_record_interaction_tool(
        score: int,
        entry_ids: list[int],
        runtime: ToolRuntime,
        summary: str | None = None,
    ) -> Command:
        review_state = _require_review_state(runtime)
        if review_state is None:
            return _not_initialized_command(runtime.tool_call_id)

        session_id = review_state["session_id"]
        interaction_count = review_state["interaction_count"]
        position = interaction_count + 1

        # Write ReviewInteraction + ReviewInteractionEntry DB records
        async with session_factory() as session:
            await add_review_interaction(
                session,
                session_id=session_id,
                entry_ids=list(entry_ids),
                summary=summary,
                score=score,
                position=position,
            )
            await session.commit()

        # Update ReviewState (partial — only the keys this call mutates).
        new_coverage = dict(review_state["entry_coverage"])
        for eid in entry_ids:
            new_coverage[eid] = new_coverage.get(eid, 0) + 1

        # Build tool message
        total_entries = len(new_coverage)
        touched = sum(1 for c in new_coverage.values() if c > 0)
        untouched_ids = [eid for eid, c in new_coverage.items() if c == 0]

        parts = [f"Recorded #{position} (score: {score}/4)."]
        parts.append(f"Coverage: {touched}/{total_entries} entries touched.")

        if untouched_ids:
            parts.append(f"Untouched: {untouched_ids}.")
        else:
            parts.append("All entries covered at least once.")

        msg = " ".join(parts)
        return Command(update={
            "review": {
                "entry_coverage": new_coverage,
                "interaction_count": position,
            },
            "messages": [ToolMessage(content=msg, tool_call_id=runtime.tool_call_id)],
        })

    # -------------------------------------------------------------------
    # review_present_flashcards
    # -------------------------------------------------------------------

    @tool_visibility(ToolVisibility.LOW)
    @tool("review_present_flashcards", description=(
        "Present flashcards to the user via the FlashcardReview widget. "
        "All queued flashcards are shown together; the user works through "
        "the whole batch before the widget resolves. Pass flashcard_ids to "
        "override the queue with an explicit subset.\n\n"
        "The widget handles rating (manual 1-4, in-widget AGAIN requeue, "
        "and optional auto-scoring) entirely in memory. On resolve, this "
        "tool commits each card's final FSRS state to the DB and records "
        "review interactions — both gated on the session not being "
        "ephemeral. Queue reconciliation per outcome:\n"
        "- easy/good/hard -> interaction recorded, removed from queue.\n"
        "- again          -> no interaction (FSRS state committed); "
        "left in queue so the agent can re-present later if desired.\n"
        "- skipped        -> left in queue; agent decides whether to drop.\n"
        "- auto-pending   -> session ended mid-auto-score batch; left in queue.\n"
        "- untouched      -> session cancelled before the card was rated; "
        "left in queue.\n\n"
        "Cards the user flagged (alt+m in the widget) are surfaced under a "
        "separate 'flagged' slot regardless of outcome — these are cards the "
        "user wants you to take another look at, potentially to request "
        "edits or revisit later.\n\n"
        "If the session was cancelled, partial results are still returned."
    ))
    async def review_present_flashcards_tool(
        runtime: ToolRuntime,
        flashcard_ids: list[int] | None = None,
    ) -> Command:
        review_state = _require_review_state(runtime)
        if review_state is None:
            return _not_initialized_command(runtime.tool_call_id)

        queue = list(review_state["flashcard_queue"])

        # Determine which flashcards to present
        if flashcard_ids is not None:
            ids_to_present = list(flashcard_ids)
        else:
            if not queue:
                return Command(update={
                    "messages": [ToolMessage(
                        content="Error: flashcard queue is empty and no flashcard_ids provided.",
                        tool_call_id=runtime.tool_call_id,
                    )],
                })
            ids_to_present = list(queue)

        # Fetch flashcard data from DB
        async with session_factory() as session:
            flashcards = await get_flashcards_by_ids(session, ids_to_present)

        if not flashcards:
            return Command(update={
                "messages": [ToolMessage(
                    content="Error: no flashcards found for the given IDs.",
                    tool_call_id=runtime.tool_call_id,
                )],
            })

        flashcard_map = {fc.id: fc for fc in flashcards}

        # Build card data for the widget. ``fsrs_card`` carries the
        # session-start FSRS state into the widget, which then mutates a
        # private copy in memory. The widget never persists those
        # mutations itself — see the result-handling block below for the
        # commit-on-non-ephemeral path.
        card_data = [
            {
                "id": fc.id,
                "question": fc.question_text,
                "answer": fc.answer_text,
                "testing_notes": fc.testing_notes,
                "fsrs_card": to_fsrs_card(fc),
            }
            for fc in flashcards
        ]

        result = interrupt({
            "type": "flashcard_review",
            "cards": card_data,
            "auto_score_enabled": True,
        })

        # Process results
        new_queue = list(review_state["flashcard_queue"])
        new_coverage = dict(review_state["entry_coverage"])
        interaction_count = review_state["interaction_count"]
        session_id = review_state["session_id"]

        scored_ids: list[int] = []
        again_ids: list[int] = []
        skipped_ids: list[int] = []
        auto_pending_ids: list[int] = []
        untouched_ids: list[int] = []
        # Flag is orthogonal to the score outcome — a flagged card may also
        # appear in any of the buckets above. Collected separately so the
        # agent sees it as a distinct call to action.
        flagged_ids: list[int] = []
        user_answers: dict[int, str] = {}

        def _drop_from_queue(fc_id: int) -> None:
            if fc_id in new_queue:
                new_queue.remove(fc_id)

        # Ephemeral sessions are not persisted — skip FSRS writes and
        # interaction records. ``config`` may be None if the user never
        # called review_update_session_state.
        config = review_state.get("config") or {}
        ephemeral = bool(config.get("ephemeral", False))

        for card_result in result["cards"]:
            fc_id = card_result["id"]
            fc = flashcard_map.get(fc_id)
            if fc is None:
                continue

            label = card_result.get("score_label")
            score = card_result.get("score")
            user_answer = card_result.get("user_answer", "")
            if user_answer:
                user_answers[fc_id] = user_answer
            if card_result.get("flagged"):
                flagged_ids.append(fc_id)

            # FSRS state lives in the widget's in-memory snapshot and
            # arrives back here as ``fsrs_card``. The widget never wrote
            # it to the DB; we do that here, but only for non-ephemeral
            # sessions. Cards with no rating change (untouched, skipped
            # from FRONT) carry their initial state, so committing is a
            # no-op equivalent.
            fsrs_card = card_result.get("fsrs_card")
            if not ephemeral and fsrs_card is not None:
                async with session_factory() as session:
                    await commit_fsrs_card(session, fc_id, fsrs_card)
                    await session.commit()

            if label in ("easy", "good", "hard"):
                entry_ids = [fe.entry_id for fe in fc.flashcard_entries]
                interaction_count += 1
                if not ephemeral:
                    async with session_factory() as session:
                        await add_review_interaction(
                            session,
                            session_id=session_id,
                            entry_ids=entry_ids,
                            score=score,
                            position=interaction_count,
                            flashcard_id=fc_id,
                        )
                        await session.commit()
                for eid in entry_ids:
                    new_coverage[eid] = new_coverage.get(eid, 0) + 1
                scored_ids.append(fc_id)
                _drop_from_queue(fc_id)
            elif label == "again":
                # Widget cycled the card in-session and applied
                # Rating.Again to the in-memory FSRS state (now committed
                # above for non-ephemeral). Reaching the tool with an
                # AGAIN label means the in-widget requeue was interrupted
                # — leave the card in the queue, no interaction recorded.
                again_ids.append(fc_id)
            elif label == "skipped":
                skipped_ids.append(fc_id)
            elif label == "auto":
                # Cancelled mid-auto-score batch — no rating finalized.
                auto_pending_ids.append(fc_id)
            else:
                # label is None — card never reached a terminal score
                # (cancelled while still on FRONT or REVEALED_NOT_SCORED).
                untouched_ids.append(fc_id)

        # Partial review-state update — only the keys this call mutates.
        review_partial: dict = {
            "flashcard_queue": new_queue,
            "entry_coverage": new_coverage,
            "interaction_count": interaction_count,
        }

        # Build summary message
        parts: list[str] = []
        completed = result.get("completed", False)
        parts.append(
            "Flashcard session complete." if completed else "Flashcard session cancelled."
        )

        if scored_ids:
            parts.append(f"{len(scored_ids)} card(s) scored and recorded: {scored_ids}.")
        if again_ids:
            parts.append(
                f"{len(again_ids)} card(s) ended on AGAIN (left in queue, FSRS updated): {again_ids}."
            )
        if skipped_ids:
            parts.append(f"{len(skipped_ids)} card(s) skipped (left in queue): {skipped_ids}.")
        if auto_pending_ids:
            parts.append(
                f"{len(auto_pending_ids)} card(s) pending auto-score at cancel "
                f"(left in queue): {auto_pending_ids}."
            )
        if untouched_ids:
            parts.append(f"{len(untouched_ids)} card(s) untouched (left in queue): {untouched_ids}.")

        if flagged_ids:
            parts.append(
                f"\nUser flagged the following card(s), potentially to request "
                f"changes or take another look: {flagged_ids}."
            )

        if user_answers:
            parts.append("\nUser answers:")
            for fc_id, answer in user_answers.items():
                fc = flashcard_map.get(fc_id)
                q = fc.question_text if fc else "?"
                parts.append(f"  - Flashcard {fc_id} (Q: {q}): {answer}")

        if new_queue:
            parts.append(f"Flashcard queue: {len(new_queue)} remaining.")
        else:
            parts.append("Flashcard queue fully drained.")

        msg = " ".join(parts)
        return Command(update={
            "review": review_partial,
            "messages": [ToolMessage(content=msg, tool_call_id=runtime.tool_call_id)],
        })

    # -------------------------------------------------------------------
    # review_finish_session
    # -------------------------------------------------------------------

    @tool_visibility(ToolVisibility.DEFAULT)
    @tool("review_finish_session", description=(
        "Finalize the review session: compute aggregate stats, build and persist "
        "the session summary (combining stats with the agent's observations), "
        "mark the session as completed, and clear the review state. "
        "Pass agent_summary with your observations about the session "
        "(strengths, weaknesses, recommendations). Returns the computed stats."
    ))
    async def review_finish_session_tool(
        runtime: ToolRuntime,
        agent_summary: str | None = None,
    ) -> Command:
        review_state = _require_review_state(runtime)
        if review_state is None:
            return _not_initialized_command(runtime.tool_call_id)

        session_id = review_state["session_id"]

        # Compute stats and mark complete
        async with session_factory() as session:
            stats = await get_interaction_stats(session, session_id)
            await complete_review_session(session, session_id)
            await session.commit()

        # Build final summary
        summary_parts: list[str] = []

        # Stats section
        avg_score = stats['average_score']
        summary_parts.append(f"Total interactions: {stats['total']}")
        summary_parts.append(f"Scored interactions: {stats['scored']}")
        summary_parts.append(f"Average score: {avg_score}/4" if avg_score is not None else "Average score: N/A")

        if stats["per_entry"]:
            summary_parts.append("\nPer-entry breakdown:")
            for eid, entry_stats in sorted(stats["per_entry"].items()):
                avg = round(entry_stats["total_score"] / entry_stats["scored"], 2) if entry_stats["scored"] > 0 else "N/A"
                summary_parts.append(f"  Entry [{eid}]: {entry_stats['count']} interactions, avg score: {avg}")

        # Agent observations
        if agent_summary:
            summary_parts.append(f"\nAgent observations:\n{agent_summary}")

        final_summary = "\n".join(summary_parts)

        # Persist summary (for non-ephemeral sessions)
        config = review_state.get("config")
        is_ephemeral = config.get("ephemeral", False) if config else False
        if not is_ephemeral:
            async with session_factory() as session:
                await update_session_summary(session, session_id, final_summary)
                await session.commit()

        # Build return message (stats for the agent to present to the user)
        stat_lines = [
            f"Total interactions: {stats['total']}",
            f"Scored interactions: {stats['scored']}",
            f"Average score: {avg_score}/4" if avg_score is not None else "Average score: N/A",
        ]

        if stats["per_entry"]:
            stat_lines.append("\nPer-entry breakdown:")
            for eid, entry_stats in sorted(stats["per_entry"].items()):
                avg = round(entry_stats["total_score"] / entry_stats["scored"], 2) if entry_stats["scored"] > 0 else "N/A"
                stat_lines.append(f"  Entry [{eid}]: {entry_stats['count']} interactions, avg score: {avg}")

        msg = "Review session finalized.\n\n" + "\n".join(stat_lines)
        if is_ephemeral:
            msg += "\n\n(Ephemeral session — summary not persisted.)"

        return Command(update={
            "review": None,
            "messages": [ToolMessage(content=msg, tool_call_id=runtime.tool_call_id)],
        })

    return {
        "review_get_past_sessions": review_get_past_sessions_tool,
        "review_show_session_state": review_show_session_state_tool,
        "review_start_session": review_start_session_tool,
        "review_update_session_state": review_update_session_state_tool,
        "review_record_interaction": review_record_interaction_tool,
        "review_present_flashcards": review_present_flashcards_tool,
        "review_finish_session": review_finish_session_tool,
    }
