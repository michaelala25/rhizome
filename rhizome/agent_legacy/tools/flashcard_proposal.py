"""Flashcard proposal tools — stage, present, and accept flashcard proposals.

These tools are mode-independent: they can be used during learn mode
(to create flashcards from a learning conversation) or during review mode
(to propose flashcards as part of a review session).  Proposal state is
stored in ``RhizomeAgentState.flashcard_proposal_state``, separate from
``ReviewState``.
"""

from __future__ import annotations

import json
from typing import Any

from langchain.tools import tool
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from rhizome.agent_legacy.state import FlashcardProposalItem, FlashcardProposalState
from rhizome.agent_legacy.tools.visibility import ToolVisibility, tool_visibility
from rhizome.db.operations import create_flashcard
from rhizome.logs import get_logger

_logger = get_logger("agent.flashcard_proposal_tools")


# ---------------------------------------------------------------------------
# Validation helper
# ---------------------------------------------------------------------------

class _ValidationResult:
    """Result of running the answerer/comparator validation pipeline."""

    __slots__ = ("all_passed", "passed", "failed", "total", "results", "error")

    def __init__(
        self,
        *,
        all_passed: bool = False,
        passed: int = 0,
        failed: int = 0,
        total: int = 0,
        results: list[dict[str, Any]] | None = None,
        error: str | None = None,
    ) -> None:
        self.all_passed = all_passed
        self.passed = passed
        self.failed = failed
        self.total = total
        self.results = results or []
        self.error = error

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "all_passed": self.all_passed,
            "passed": self.passed,
            "failed": self.failed,
            "total": self.total,
            "results": self.results,
        }
        if self.error:
            d["error"] = self.error
        return d


async def _validate_flashcards(
    items: list[FlashcardProposalItem],
    answerer,
    comparator,
) -> _ValidationResult:
    """Run the answerer → comparator validation pipeline on *items*.

    Returns a ``_ValidationResult`` with per-card pass/fail and feedback.
    On subagent failure, ``result.error`` is set and card-level results
    may be empty.
    """
    # Step 1: Build question list for the answerer
    questions_payload = [
        {"index": i, "question": fc["question_text"]}
        for i, fc in enumerate(items)
    ]

    answerer_input = (
        "Answer each of the following flashcard questions:\n\n"
        + "\n".join(f"{q['index']}. {q['question']}" for q in questions_payload)
    )

    _logger.debug("Invoking answerer subagent with %d question(s)", len(questions_payload))
    _, answerer_response, _ = await answerer.ainvoke(answerer_input)

    if answerer.structured_response is None:
        return _ValidationResult(
            error="Answerer subagent failed to produce structured output.",
            total=len(items),
        )

    answerer_answers: dict[int, str] = {
        a.question_index: a.answer
        for a in answerer.structured_response.answers
    }

    # Step 2: Build comparison payload
    comparison_items = [
        {
            "index": i,
            "question": fc["question_text"],
            "expected_answer": fc["answer_text"],
            "test_taker_answer": answerer_answers.get(i, "(no answer provided)"),
            "testing_notes": fc.get("testing_notes"),
        }
        for i, fc in enumerate(items)
    ]

    comparator_input = (
        "Evaluate the following flashcards for clarity and unambiguity:\n\n"
        + "\n---\n".join(
            f"Card {item['index']}:\n"
            f"  Question: {item['question']}\n"
            f"  Expected answer: {item['expected_answer']}\n"
            f"  Test-taker answer: {item['test_taker_answer']}\n"
            + (f"  Testing notes: {item['testing_notes']}\n" if item["testing_notes"] else "")
            for item in comparison_items
        )
    )

    _logger.debug("Invoking comparator subagent with %d card(s)", len(comparison_items))
    _, comparator_response, _ = await comparator.ainvoke(comparator_input)

    if comparator.structured_response is None:
        return _ValidationResult(
            error="Comparator subagent failed to produce structured output.",
            total=len(items),
        )

    # Step 3: Build result summary
    results = []
    all_passed = True
    for card_result in comparator.structured_response.results:
        idx = card_result.question_index
        fc = items[idx] if idx < len(items) else None
        result_entry: dict[str, Any] = {
            "question_index": idx,
            "question": fc["question_text"] if fc else "(unknown)",
            "expected_answer": fc["answer_text"] if fc else "(unknown)",
            "test_taker_answer": answerer_answers.get(idx, "(no answer)"),
            "passed": card_result.passed,
            "feedback": card_result.feedback,
        }
        results.append(result_entry)
        if not card_result.passed:
            all_passed = False

    passed_count = sum(1 for r in results if r["passed"])
    failed_count = len(results) - passed_count

    _logger.info("Flashcard validation: %d/%d passed", passed_count, len(results))

    return _ValidationResult(
        all_passed=all_passed,
        passed=passed_count,
        failed=failed_count,
        total=len(results),
        results=results,
    )


class FlashcardInput(BaseModel):
    """Input schema for creating a single flashcard."""

    topic_id: int = Field(description="Topic ID the flashcard belongs to")
    question_text: str = Field(description="The question text")
    answer_text: str = Field(description="The expected answer text")
    entry_ids: list[int] = Field(description="Knowledge entry IDs this flashcard tests")
    testing_notes: str | None = Field(default=None, description="Notes on how to assess responses")


class FlashcardEdit(BaseModel):
    """Partial update to a single flashcard in the proposal."""
    id: int = Field(description="Stable ID of the flashcard to edit")
    question_text: str | None = Field(default=None, description="New question text (omit to keep current)")
    answer_text: str | None = Field(default=None, description="New answer text (omit to keep current)")
    testing_notes: str | None = Field(default=None, description="New testing notes (omit to keep current)")


# ---------------------------------------------------------------------------
# Diff helper
# ---------------------------------------------------------------------------

def _build_flashcard_diff(
    original: list[FlashcardProposalItem],
    returned: list[dict],
    originals_by_id: dict[int, FlashcardProposalItem],
) -> list[str]:
    """Compare original proposal items against widget-returned cards.

    Returns a list of human-readable lines describing exclusions and edits.
    """
    returned_ids = {r["id"] for r in returned}
    original_ids = {fc["id"] for fc in original}

    parts: list[str] = []

    # Exclusions
    excluded_ids = sorted(original_ids - returned_ids)
    if excluded_ids:
        labels = [f"card {cid}" for cid in excluded_ids]
        parts.append(f"Excluded by user: {', '.join(labels)}")

    # Per-card edits
    for returned_card in returned:
        card_id = returned_card["id"]
        orig = originals_by_id[card_id]
        changed: list[str] = []
        if returned_card["question"] != orig["question_text"]:
            changed.append("question")
        if returned_card["answer"] != orig["answer_text"]:
            changed.append("answer")
        if returned_card.get("testing_notes") != orig.get("testing_notes"):
            changed.append("testing_notes")
        if changed:
            parts.append(f"Card {card_id}: user edited {', '.join(changed)}")

    if not parts:
        parts.append("No direct edits or exclusions by user.")

    return parts


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------

def build_flashcard_proposal_tools(
    session_factory,
    answerer=None,
    comparator=None,
) -> dict[str, Any]:
    """Build flashcard proposal tools with session_factory closed over.

    Parameters
    ----------
    answerer, comparator:
        Optional ``StructuredSubagent`` instances for flashcard validation.
        Required if the ``validate`` flag on ``flashcard_proposal_create``
        or ``flashcard_proposal_edit`` is to be used.
    """

    @tool("flashcard_proposal_create", description=(
        "Stage flashcards for user review without writing to the database. "
        "Stores the proposal in agent state. Call flashcard_proposal_present "
        "next to show it to the user. Each flashcard needs: topic_id, "
        "question_text, answer_text, entry_ids, and optionally testing_notes. "
        "Set validate=True to run an automated clarity check before presenting "
        "to the user (required on first call; optional on subsequent re-stages)."
    ))
    @tool_visibility(ToolVisibility.LOW)
    async def create_flashcard_proposal_tool(
        flashcards: list[FlashcardInput],
        runtime: ToolRuntime,
        validate: bool = False,
    ) -> Command:
        items: list[FlashcardProposalItem] = [
            FlashcardProposalItem(
                id=i,
                topic_id=fc.topic_id,
                question_text=fc.question_text,
                answer_text=fc.answer_text,
                entry_ids=list(fc.entry_ids),
                testing_notes=fc.testing_notes,
            )
            for i, fc in enumerate(flashcards)
        ]

        proposal_state = FlashcardProposalState(items=items)

        if not validate:
            msg = (
                f"Flashcard proposal staged: {len(items)} card(s). "
                f"Call flashcard_proposal_present to show it to the user."
            )
            return Command(update={
                "flashcard_proposal_state": proposal_state,
                "messages": [ToolMessage(content=msg, tool_call_id=runtime.tool_call_id)],
            })

        # --- Inline validation ---
        if answerer is None or comparator is None:
            return Command(update={
                "flashcard_proposal_state": proposal_state,
                "messages": [ToolMessage(
                    content="Error: validation subagents not configured. Stage without validate=True.",
                    tool_call_id=runtime.tool_call_id,
                )],
            })

        vr = await _validate_flashcards(items, answerer, comparator)

        if vr.error:
            return Command(update={
                "flashcard_proposal_state": proposal_state,
                "messages": [ToolMessage(
                    content=json.dumps({"error": vr.error}, indent=2),
                    tool_call_id=runtime.tool_call_id,
                )],
            })

        if vr.all_passed:
            msg = (
                f"Flashcard proposal staged and validated: "
                f"all {vr.total} card(s) are clear and unambiguous. "
                f"Proceed with flashcard_proposal_present."
            )
        else:
            msg = (
                f"Flashcard proposal staged. Validation: "
                f"{vr.passed}/{vr.total} passed, {vr.failed} failed. "
                f"Review the feedback, revise failed cards with "
                f"flashcard_proposal_edit(edits=..., validate=True)."
            )

        return Command(update={
            "flashcard_proposal_state": proposal_state,
            "messages": [ToolMessage(
                content=json.dumps({"summary": msg, **vr.to_dict()}, indent=2),
                tool_call_id=runtime.tool_call_id,
            )],
        })

    @tool("flashcard_proposal_present", description=(
        "Display the staged flashcard proposal to the user for review. "
        "The user can approve, request edits, reset, or cancel. "
        "Returns the user's choice. If approved, call flashcard_proposal_accept "
        "to write them to the database. If edits requested, use "
        "flashcard_proposal_edit to make targeted changes (preserving any "
        "direct edits the user made), then present again."
    ))
    @tool_visibility(ToolVisibility.LOW)
    async def present_flashcard_proposal_tool(
        runtime: ToolRuntime,
    ) -> Command:
        fp_state: FlashcardProposalState | None = runtime.state.get("flashcard_proposal_state")

        if not fp_state or not fp_state.get("items"):
            return Command(update={
                "messages": [ToolMessage(
                    content="Error: no flashcard proposal staged. Call flashcard_proposal_create first.",
                    tool_call_id=runtime.tool_call_id,
                )],
            })

        proposal = fp_state["items"]

        # Build the interrupt payload matching FlashcardProposal.from_interrupt
        interrupt_flashcards = [
            {
                "id": fc["id"],
                "question": fc["question_text"],
                "answer": fc["answer_text"],
                "testing_notes": fc.get("testing_notes"),
                "entry_ids": fc.get("entry_ids", []),
            }
            for fc in proposal
        ]

        result = interrupt({
            "type": "flashcard_proposal",
            "flashcards": interrupt_flashcards,
        })

        choice = result["choice"]
        returned_cards = result.get("flashcards", [])

        # Build updated items from returned cards, keyed by stable id
        originals_by_id = {fc["id"]: fc for fc in proposal}
        updated_items: list[FlashcardProposalItem] = []
        for returned in returned_cards:
            card_id = returned["id"]
            original = originals_by_id[card_id]
            updated_items.append(FlashcardProposalItem(
                id=card_id,
                topic_id=original["topic_id"],
                question_text=returned["question"],
                answer_text=returned["answer"],
                entry_ids=original["entry_ids"],
                testing_notes=returned.get("testing_notes"),
            ))

        # Build diff summary
        diff_parts = _build_flashcard_diff(proposal, returned_cards, originals_by_id)

        if choice == "Approve":
            msg_lines = [
                f"User approved {len(updated_items)} flashcard(s).",
                *diff_parts,
                "Call flashcard_proposal_accept to write them to the database.",
            ]
            return Command(update={
                "flashcard_proposal_state": {**fp_state, "items": updated_items},
                "messages": [ToolMessage(content="\n".join(msg_lines), tool_call_id=runtime.tool_call_id)],
            })

        elif choice == "Edit":
            instructions = result.get("instructions", "")
            msg_lines = [
                f"User requested edits: {instructions}",
                *diff_parts,
                f"Proposal state updated ({len(updated_items)} card(s) remaining).",
                "Use flashcard_proposal_edit to make further changes, then "
                "flashcard_proposal_present to show the revised proposal.",
            ]
            return Command(update={
                "flashcard_proposal_state": {**fp_state, "items": updated_items},
                "messages": [ToolMessage(content="\n".join(msg_lines), tool_call_id=runtime.tool_call_id)],
            })

        else:  # Cancel
            msg = "User cancelled the flashcard proposal."
            return Command(update={
                "flashcard_proposal_state": None,
                "messages": [ToolMessage(content=msg, tool_call_id=runtime.tool_call_id)],
            })

    @tool("flashcard_proposal_edit", description=(
        "Make targeted edits to the current flashcard proposal without overwriting it. "
        "Supports in-place edits (partial field updates by stable ID), deletions (by ID), "
        "and additions (new flashcards appended with auto-assigned IDs). "
        "Processing order: edits, then deletions, then additions. "
        "Set validate=True to run an automated clarity check on only the "
        "edited and added cards (unchanged cards are not re-validated). "
        "Call flashcard_proposal_present afterwards to show the revised proposal to the user."
    ))
    @tool_visibility(ToolVisibility.LOW)
    async def edit_flashcard_proposal_tool(
        runtime: ToolRuntime,
        edits: list[FlashcardEdit] | None = None,
        additions: list[FlashcardInput] | None = None,
        deletions: list[int] | None = None,
        validate: bool = False,
    ) -> Command:
        fp_state: FlashcardProposalState | None = runtime.state.get("flashcard_proposal_state")

        if not fp_state or not fp_state.get("items"):
            return Command(update={
                "messages": [ToolMessage(
                    content="Error: no flashcard proposal to edit. Create one first.",
                    tool_call_id=runtime.tool_call_id,
                )],
            })

        items = [dict(item) for item in fp_state["items"]]
        items_by_id = {item["id"]: item for item in items}
        changes: list[str] = []
        touched_ids: set[int] = set()

        # 1. Apply edits (by stable id)
        for edit in (edits or []):
            item = items_by_id.get(edit.id)
            if item is None:
                continue
            if edit.question_text is not None:
                item["question_text"] = edit.question_text
            if edit.answer_text is not None:
                item["answer_text"] = edit.answer_text
            if edit.testing_notes is not None:
                item["testing_notes"] = edit.testing_notes
            changes.append(f"edited card {edit.id}")
            touched_ids.add(edit.id)

        # 2. Apply deletions (by stable id)
        delete_ids = set(deletions or [])
        for did in sorted(delete_ids):
            if did in items_by_id:
                changes.append(f"deleted card {did} ({items_by_id[did]['question_text'][:40]!r})")
        items = [item for item in items if item["id"] not in delete_ids]

        # 3. Append additions (assign next available id)
        next_id = max((item["id"] for item in fp_state["items"]), default=-1) + 1
        for addition in (additions or []):
            items.append(FlashcardProposalItem(
                id=next_id,
                topic_id=addition.topic_id,
                question_text=addition.question_text,
                answer_text=addition.answer_text,
                entry_ids=list(addition.entry_ids),
                testing_notes=addition.testing_notes,
            ))
            changes.append(f"added card {next_id} ({addition.question_text[:40]!r})")
            touched_ids.add(next_id)
            next_id += 1

        updated_state = {**fp_state, "items": items}
        edit_summary = "; ".join(changes) if changes else "no changes applied"

        # 4. Optional validation of only touched cards
        if validate and touched_ids:
            if answerer is None or comparator is None:
                return Command(update={
                    "flashcard_proposal_state": updated_state,
                    "messages": [ToolMessage(
                        content=(
                            f"Flashcard proposal updated ({len(items)} card(s)): {edit_summary}. "
                            f"Error: validation subagents not configured."
                        ),
                        tool_call_id=runtime.tool_call_id,
                    )],
                })

            items_to_validate = [item for item in items if item["id"] in touched_ids]
            vr = await _validate_flashcards(items_to_validate, answerer, comparator)

            if vr.error:
                return Command(update={
                    "flashcard_proposal_state": updated_state,
                    "messages": [ToolMessage(
                        content=json.dumps({
                            "edit_summary": edit_summary,
                            "error": vr.error,
                        }, indent=2),
                        tool_call_id=runtime.tool_call_id,
                    )],
                })

            # Remap validation indices back to stable card IDs
            validated_ids = [item["id"] for item in items_to_validate]
            for r in vr.results:
                r["card_id"] = validated_ids[r["question_index"]]

            if vr.all_passed:
                msg = (
                    f"Flashcard proposal updated ({len(items)} card(s)): {edit_summary}. "
                    f"Validation: all {vr.total} edited/added card(s) passed. "
                    f"Proceed with flashcard_proposal_present."
                )
            else:
                msg = (
                    f"Flashcard proposal updated ({len(items)} card(s)): {edit_summary}. "
                    f"Validation: {vr.passed}/{vr.total} edited/added card(s) passed, "
                    f"{vr.failed} failed. Review the feedback and revise with "
                    f"flashcard_proposal_edit(edits=..., validate=True)."
                )

            return Command(update={
                "flashcard_proposal_state": updated_state,
                "messages": [ToolMessage(
                    content=json.dumps({"summary": msg, **vr.to_dict()}, indent=2),
                    tool_call_id=runtime.tool_call_id,
                )],
            })

        msg = f"Flashcard proposal updated ({len(items)} card(s)): {edit_summary}."
        return Command(update={
            "flashcard_proposal_state": updated_state,
            "messages": [ToolMessage(content=msg, tool_call_id=runtime.tool_call_id)],
        })

    @tool("flashcard_proposal_accept", description=(
        "Write the approved flashcard proposal to the database. "
        "Call this after the user has approved via flashcard_proposal_present. "
        "Returns the created flashcard IDs."
    ))
    @tool_visibility(ToolVisibility.LOW)
    async def accept_flashcard_proposal_tool(
        runtime: ToolRuntime,
    ) -> Command:
        fp_state: FlashcardProposalState | None = runtime.state.get("flashcard_proposal_state")

        if not fp_state or not fp_state.get("items"):
            return Command(update={
                "messages": [ToolMessage(
                    content="Error: no flashcard proposal to accept. Stage and present a proposal first.",
                    tool_call_id=runtime.tool_call_id,
                )],
            })

        proposal = fp_state["items"]

        # Use review session ID if one is active, otherwise None
        review_state = runtime.state.get("review")
        session_id = review_state["session_id"] if review_state else None

        new_ids: list[int] = []
        async with session_factory() as session:
            for fc_item in proposal:
                fc = await create_flashcard(
                    session,
                    topic_id=fc_item["topic_id"],
                    question_text=fc_item["question_text"],
                    answer_text=fc_item["answer_text"],
                    entry_ids=fc_item["entry_ids"],
                    testing_notes=fc_item.get("testing_notes"),
                    session_id=session_id,
                )
                new_ids.append(fc.id)
            await session.commit()

        msg = f"Created {len(new_ids)} flashcard(s) (IDs: {new_ids})."
        return Command(update={
            "flashcard_proposal_state": None,
            "messages": [ToolMessage(content=msg, tool_call_id=runtime.tool_call_id)],
        })

    return {
        "flashcard_proposal_create": create_flashcard_proposal_tool,
        "flashcard_proposal_present": present_flashcard_proposal_tool,
        "flashcard_proposal_edit": edit_flashcard_proposal_tool,
        "flashcard_proposal_accept": accept_flashcard_proposal_tool,
    }
