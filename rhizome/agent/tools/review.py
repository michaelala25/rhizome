"""Review-mode tools for review sessions.

Each tool creates its own DB session via a closure over ``session_factory``,
matching the pattern in other tool modules.  Tools that mutate ReviewState
return ``Command(update={"review": ...})``.
"""

from __future__ import annotations

from typing import Literal

from fsrs import Rating
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
    apply_rating,
    complete_review_session,
    create_review_session,
    get_flashcard_entry_ids,
    get_flashcards_by_ids,
    get_interaction_stats,
    get_sessions_by_topics,
    update_session_ephemeral,
    update_session_instructions,
    update_session_plan,
    update_session_summary,
)
from rhizome.logs import get_logger

_logger = get_logger("agent.review_tools")

# Match the constant in FlashcardReview to avoid cross-layer import.
AUTO_SCORE = -1


# ---------------------------------------------------------------------------
# Pydantic schemas for review_update_session_state
# ---------------------------------------------------------------------------

class ReviewConfigUpdate(BaseModel):
    """Partial update to review configuration.  Only provided fields are applied."""
    style: str | None = Field(default=None, description="Review style: 'flashcard', 'conversation', or 'mixed'")
    critique_timing: str | None = Field(default=None, description="When to deliver critique: 'during' or 'after'")
    question_source: str | None = Field(default=None, description="Question source: 'existing', 'generated', or 'both'")
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


async def _ensure_review_state(
    runtime: ToolRuntime,
    session_factory,
) -> tuple[ReviewState, bool]:
    """Return the current ReviewState, lazily initializing if needed.

    Returns (state, was_created).
    """
    existing: ReviewState | None = runtime.state.get("review")
    if existing is not None:
        return existing, False

    # Create a bare DB ReviewSession
    async with session_factory() as session:
        review_session = await create_review_session(
            session, topic_ids=[], entry_ids=[],
        )
        await session.commit()
        session_id = review_session.id

    return _empty_review_state(session_id), True


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

def build_review_tools(session_factory, scorer=None) -> dict:
    """Build all review-mode tool functions with session_factory closed over."""

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
            lines.append(
                f"Config: style={config['style']}, timing={config['critique_timing']}, "
                f"source={config['question_source']}, ephemeral={config['ephemeral']}"
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
    # review_update_session_state
    # -------------------------------------------------------------------

    @tool_visibility(ToolVisibility.LOW)
    @tool("review_update_session_state", description=(
        "Update the review session state. Lazily initializes the session on first call. "
        "All parameters are optional — only provided values are applied.\n\n"
        "- scope: list of entry_ids to set as the review scope (derives topic_ids automatically).\n"
        "- config: partial config update (style, critique_timing, question_source, ephemeral, user_instructions).\n"
        "- flashcards: update the flashcard queue (append/set/remove/clear).\n"
        "- plan: set the discussion plan for conversational review.\n"
        "- clear: abandon the session and clear all state (DB records remain)."
    ))
    async def review_update_session_state_tool(
        runtime: ToolRuntime,
        scope: list[int] | None = None,
        config: ReviewConfigUpdate | None = None,
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

        # -- Lazy init --
        review_state, was_created = await _ensure_review_state(runtime, session_factory)
        new_state = dict(review_state)
        session_id = new_state["session_id"]
        results: list[str] = []

        if was_created:
            results.append(f"Review session initialized (DB session #{session_id}).")

        # -- Scope --
        if scope is not None:
            entry_ids = list(scope)
            # Derive topic_ids from entries
            async with session_factory() as session:
                result = await session.execute(
                    select(KnowledgeEntry.topic_id)
                    .where(KnowledgeEntry.id.in_(entry_ids))
                    .distinct()
                )
                topic_ids = list(result.scalars().all())

            # Update DB junction rows
            await _update_scope_in_db(session_factory, session_id, topic_ids, entry_ids)

            new_state["scope"] = ReviewScope(topic_ids=topic_ids, entry_ids=entry_ids)
            new_state["entry_coverage"] = {eid: new_state["entry_coverage"].get(eid, 0) for eid in entry_ids}
            results.append(f"Scope set: {len(entry_ids)} entries across {len(topic_ids)} topics.")

        # -- Config --
        if config is not None:
            existing_config = new_state.get("config") or {}
            updated = dict(existing_config)

            if config.style is not None:
                updated["style"] = config.style
            if config.critique_timing is not None:
                updated["critique_timing"] = config.critique_timing
            if config.question_source is not None:
                updated["question_source"] = config.question_source
            if config.ephemeral is not None:
                updated["ephemeral"] = config.ephemeral
                async with session_factory() as session:
                    await update_session_ephemeral(session, session_id, config.ephemeral)
                    await session.commit()
            if config.user_instructions is not None:
                updated["user_instructions"] = config.user_instructions
                async with session_factory() as session:
                    await update_session_instructions(session, session_id, config.user_instructions)
                    await session.commit()

            new_state["config"] = ReviewConfig(**updated) if updated else None

            set_fields = [k for k, v in (config.model_dump()).items() if v is not None]
            results.append(f"Config updated: {', '.join(set_fields)}.")

        # -- Flashcards --
        if flashcards is not None:
            queue = list(new_state["flashcard_queue"])
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

            new_state["flashcard_queue"] = queue

        # -- Plan --
        if plan is not None:
            async with session_factory() as session:
                await update_session_plan(session, session_id, plan)
                await session.commit()
            new_state["discussion_plan"] = plan
            results.append("Discussion plan set.")

        if not results:
            results.append("No updates applied.")

        msg = " ".join(results)
        return Command(update={
            "review": new_state,
            "messages": [ToolMessage(content=msg, tool_call_id=runtime.tool_call_id)],
        })

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
        review_state: ReviewState = runtime.state["review"]

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

        # Update ReviewState
        new_state = dict(review_state)
        new_coverage = dict(review_state["entry_coverage"])
        for eid in entry_ids:
            new_coverage[eid] = new_coverage.get(eid, 0) + 1
        new_state["entry_coverage"] = new_coverage
        new_state["interaction_count"] = position

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
            "review": new_state,
            "messages": [ToolMessage(content=msg, tool_call_id=runtime.tool_call_id)],
        })

    # -------------------------------------------------------------------
    # review_present_flashcards
    # -------------------------------------------------------------------

    @tool_visibility(ToolVisibility.LOW)
    @tool("review_present_flashcards", description=(
        "Present flashcards to the user via the FlashcardReview widget. "
        "By default pops from the queue: one card for critique-during, all "
        "cards for critique-after. Pass flashcard_ids to override. "
        "Self-scored and again cards are handled automatically; "
        "'auto' cards are scored by an internal subagent."
    ))
    async def review_present_flashcards_tool(
        runtime: ToolRuntime,
        flashcard_ids: list[int] | None = None,
    ) -> Command:
        review_state: ReviewState = runtime.state["review"]
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
            # Pop one card for critique-during, all cards for critique-after
            config = review_state.get("config")
            critique_timing = config["critique_timing"] if config else "after"
            if critique_timing == "during":
                ids_to_present = [queue[0]]
            else:
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

        # Build card data for the widget
        card_data = [
            {"id": fc.id, "question": fc.question_text, "answer": fc.answer_text}
            for fc in flashcards
        ]

        # Call interrupt to present the widget
        is_single = len(card_data) == 1
        result = interrupt({
            "type": "flashcard_review",
            "cards": card_data,
            "auto_score": True,
            "user_input_enabled": True,
            "show_complete_status": not is_single,
        })

        # Process results
        new_state = dict(review_state)
        new_queue = list(review_state["flashcard_queue"])
        new_coverage = dict(review_state["entry_coverage"])
        interaction_count = review_state["interaction_count"]
        session_id = review_state["session_id"]

        again_ids: list[int] = []
        auto_cards: list[dict] = []
        scored_count = 0
        user_answers: dict[int, str] = {}

        for card_result in result["cards"]:
            fc_id = card_result["id"]
            fc = flashcard_map.get(fc_id)
            if fc is None:
                continue

            entry_ids = [fe.entry_id for fe in fc.flashcard_entries]
            score = card_result["score"]
            user_answer = card_result.get("user_answer", "")
            user_answers[fc_id] = user_answer

            # Remove from queue regardless of score
            if fc_id in new_queue:
                new_queue.remove(fc_id)

            if score == 1:
                # "again" — requeue at end
                again_ids.append(fc_id)
            elif score == AUTO_SCORE:
                # Auto — will be scored by subagent below
                auto_cards.append({
                    "id": fc_id,
                    "question": fc.question_text,
                    "answer": fc.answer_text,
                    "user_answer": user_answer,
                    "testing_notes": fc.testing_notes,
                    "entry_ids": entry_ids,
                    "duration": card_result.get("duration"),
                })
            elif score is not None:
                # Self-scored — record interaction immediately
                interaction_count += 1
                async with session_factory() as session:
                    await add_review_interaction(
                        session,
                        session_id=session_id,
                        entry_ids=entry_ids,
                        score=score,
                        position=interaction_count,
                        flashcard_id=fc_id,
                    )
                    await apply_rating(session, fc_id, Rating(score))
                    await session.commit()
                for eid in entry_ids:
                    new_coverage[eid] = new_coverage.get(eid, 0) + 1
                scored_count += 1

        # Score auto cards via subagent
        auto_scored: list[dict] = []
        if auto_cards and scorer is not None:
            scorer_input = "Score the following flashcard answers:\n\n" + "\n---\n".join(
                f"Flashcard {ac['id']}:\n"
                f"  Question: {ac['question']}\n"
                f"  Expected answer: {ac['answer']}\n"
                f"  User's answer: {ac['user_answer'] or '(blank)'}\n"
                f"  Time spent: {ac['duration']}s\n"
                + (f"  Testing notes: {ac['testing_notes']}\n" if ac.get("testing_notes") else "")
                for ac in auto_cards
            )

            _logger.debug("Invoking scorer subagent with %d card(s)", len(auto_cards))
            _, _, _ = await scorer.ainvoke(scorer_input)

            if scorer.structured_response is not None:
                scores_by_id = {
                    r.flashcard_id: r for r in scorer.structured_response.results
                }
                auto_card_map = {ac["id"]: ac for ac in auto_cards}

                for fc_id, ac in auto_card_map.items():
                    scorer_result = scores_by_id.get(fc_id)
                    if scorer_result is None:
                        _logger.warning("Scorer did not return result for flashcard %d", fc_id)
                        continue

                    auto_score = scorer_result.score
                    feedback = scorer_result.feedback

                    if auto_score == 1:
                        again_ids.append(fc_id)
                    else:
                        interaction_count += 1
                        async with session_factory() as session:
                            await add_review_interaction(
                                session,
                                session_id=session_id,
                                entry_ids=ac["entry_ids"],
                                summary=feedback,
                                score=auto_score,
                                position=interaction_count,
                                flashcard_id=fc_id,
                            )
                            await apply_rating(session, fc_id, Rating(auto_score))
                            await session.commit()
                        for eid in ac["entry_ids"]:
                            new_coverage[eid] = new_coverage.get(eid, 0) + 1

                    auto_scored.append({
                        "id": fc_id,
                        "score": auto_score,
                        "feedback": feedback,
                    })
            else:
                _logger.warning("Scorer subagent failed to produce structured output")

        # Requeue "again" cards at end
        new_queue.extend(again_ids)

        new_state["flashcard_queue"] = new_queue
        new_state["entry_coverage"] = new_coverage
        new_state["interaction_count"] = interaction_count

        # Build summary message
        parts = []
        completed = result.get("completed", False)
        if completed:
            parts.append("Flashcard session complete.")
        else:
            parts.append("Flashcard session cancelled.")

        if user_answers:
            parts.append("\nUser answers:")
            for fc_id, answer in user_answers.items():
                fc = flashcard_map.get(fc_id)
                q = fc.question_text if fc else "?"
                parts.append(f"  - Flashcard {fc_id} (Q: {q}): {answer or '(blank)'}")
        if scored_count:
            parts.append(f"{scored_count} card(s) self-scored and recorded.")
        if auto_scored:
            parts.append(f"{len(auto_scored)} card(s) auto-scored by subagent:")
            for asc in auto_scored:
                score_labels = {1: "again", 2: "hard", 3: "good", 4: "easy"}
                label = score_labels.get(asc['score'], str(asc['score']))
                parts.append(f"  - Flashcard {asc['id']}: {label} ({asc['score']}/4) — {asc['feedback']}")
        if auto_cards and not auto_scored:
            parts.append(f"{len(auto_cards)} card(s) marked 'auto' but scorer failed — not recorded.")
        if again_ids:
            parts.append(f"{len(again_ids)} card(s) marked 'again' (requeued): {again_ids}.")
        if new_queue:
            parts.append(f"Flashcard queue: {len(new_queue)} remaining.")
        else:
            parts.append("Flashcard queue empty.")

        msg = " ".join(parts)
        return Command(update={
            "review": new_state,
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
        review_state: ReviewState = runtime.state["review"]
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
        "review_update_session_state": review_update_session_state_tool,
        "review_record_interaction": review_record_interaction_tool,
        "review_present_flashcards": review_present_flashcards_tool,
        "review_finish_session": review_finish_session_tool,
    }
