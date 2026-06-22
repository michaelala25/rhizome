"""Subagent declarations: the non-root agent kinds the runtime builds on demand.

A subagent is not a separate class anymore — it is just another ``AgentSession`` the ``AgentRuntime`` owns.
A tool spawns or resumes one by key through the live runtime on its context (``ctx.runtime.new(key)`` for a
fresh conversation, ``ctx.runtime.get(key, thread_id)`` to continue one); this module supplies the builder
behind each key, registered on the ``AgentFactory`` at composition.

The kinds:

- ``commit`` — extracts knowledge entries from selected learn-mode messages. Stateful (one thread per
  proposal), carries its own ``commit_proposal`` state, and uses read-only DB tools plus its private
  stage/edit tools. Reached by ``tools.commit.commit_invoke_subagent``.
- ``flashcard_answerer`` / ``flashcard_comparator`` — the flashcard clarity-check pipeline. Toolless,
  one-shot, structured-JSON output. Reached by ``tools.flashcard_proposal``.
- ``flashcard_scorer`` — review auto-scoring. Toolless, one-shot, structured-JSON output. Its consumer (the
  ``FlashcardReview`` widget) is not yet wired to the runtime, but the kind is registered so it can be.

The structured kinds emit a strict JSON object as their final message; ``AgentSession`` parses it into a
dict (``InvokeResult.structured_response``), which is what the consuming tool/widget reads — so they need no
``response_format``, just the strict-JSON system prompt plus the session's content parsing.

All four switch only on ``Options.Agent.Provider`` (a snapshot — a provider change rebuilds them) and pick a
sensible per-role model default; there is no per-subagent model option. Only the Anthropic provider is wired
today.

No ``from __future__ import annotations``: the runtime reads each builder's parameter annotations to wire
service / option injection, so they must be real objects, not stringized (same as ``root.py``).
"""

from dataclasses import dataclass
from typing import Annotated

from langchain.agents import create_agent
from langchain.chat_models import init_chat_model
from langchain.tools import tool
from langchain_core.messages import ToolMessage
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command

from rhizome.app.options import Options
from rhizome.credentials import APIKeyService
from rhizome.db import SessionFactoryService

from .checkpointer import AgentCheckpointerService
from .context import BaseAgentContext
from .engine import PromptCompilerMiddleware, PromptEngine
from .factory import AgentFactory
from .prompts import KNOWLEDGE_ENTRIES_GUIDE
from .state import BaseAgentState, CommitProposalEntry
from .tools import build_database_tools, render_schema_reference
from .tools.commit import COMMIT_SUBAGENT_KEY, CommitEntryEdit, KnowledgeEntryProposalSchema
from .tools.flashcard_proposal import ANSWERER_KEY, COMPARATOR_KEY

# Runtime key for the review auto-scorer. The other keys are owned by the tools that look them up; this one
# has no consumer wired yet (the FlashcardReview widget still uses the legacy scorer shape), so it lives here.
FLASHCARD_SCORER_KEY = "flashcard_scorer"


# ========================================================================================================================
# MODEL SELECTION
# ========================================================================================================================
# Per-role model defaults, keyed by provider. Only Anthropic is wired today; the OpenAI entries track
# ``Options.Agent.Model``'s choices so a provider switch still resolves to a sensible model rather than
# erroring. A subagent picks the "fast" tier for cheap high-volume work (answering, scoring) and the
# "balanced" tier where judgment matters (extraction, clarity comparison).

_FAST_MODELS = {"anthropic": "claude-haiku-4-5-20251001", "openai": "gpt-5-nano"}
_BALANCED_MODELS = {"anthropic": "claude-sonnet-4-6", "openai": "gpt-5-mini"}


def _make_model(provider: str, api_keys: APIKeyService, models: dict[str, str], temperature: float):
    """Init a chat model for ``provider`` using its entry in ``models`` (a role's tier), falling back to the
    Anthropic entry for an unmapped provider. These tiers are never Opus, so ``temperature`` is always safe
    to pass (unlike the root agent, which omits it on Opus 4.7+)."""
    model = models.get(provider, models["anthropic"])
    return init_chat_model(model, model_provider=provider, api_key=api_keys.require(provider),
                           temperature=temperature)


# ========================================================================================================================
# FLASHCARD VALIDATION / SCORING SUBAGENTS
# ========================================================================================================================
# Toolless, one-shot, structured-JSON agents. Each carries the base ``PromptEngine`` (history repair +
# payload ingestion, no request shaping) over the framework-only ``BaseAgentContext`` / ``BaseAgentState``.

ANSWERER_SYSTEM_PROMPT = """\
You are a flashcard answering agent. You will be given a set of flashcard questions.
For each question, provide your best answer using ONLY your own general knowledge.
You have NO additional context — no notes, no database, no prior conversation.

Answer each question with either:
- A single term or short phrase (preferred when the question asks for a name, command, definition, etc.)
- One short paragraph (when the question requires a brief explanation)

Do NOT hedge or say "I don't know" — always give your best attempt.

Respond ONLY with a JSON object in this exact format — no additional text:
{
    "answers": [
        {"question_index": 0, "answer": "your answer here"},
        {"question_index": 1, "answer": "your answer here"}
    ]
}"""

COMPARATOR_SYSTEM_PROMPT = """\
You are a flashcard quality evaluator. You will receive a set of flashcard questions, each with:
- The expected answer (from the flashcard author)
- An answer produced by a test-taker who had NO additional context
- Optional testing notes describing how to assess responses

Your job is to evaluate whether each flashcard is **clear and unambiguous** by comparing the \
test-taker's answer against the expected answer.

A flashcard **passes** if the test-taker's answer demonstrates that the question is clear enough \
to elicit the correct answer (or a reasonably close equivalent) without additional context. Minor \
wording differences are acceptable — focus on whether the core concept was correctly identified.

A flashcard **fails** if:
- The test-taker's answer is substantially different from the expected answer, suggesting the \
question is ambiguous or misleading
- The question could reasonably be interpreted in multiple ways, leading to a valid but different answer
- The question is too vague to elicit a specific response
- The question gives away too much of the answer, making it trivially easy (not truly testing recall)

When a flashcard fails, provide concrete, actionable suggestions for how to improve the question \
to make it unambiguous. Draw from strategies like these (use whichever are relevant):

- **Be more specific**: add qualifying context to the question to narrow the answer space \
(e.g. "In the context of X, what is Y?" instead of just "What is Y?").
- **Split into multiple cards**: if the question conflates two concepts, suggest breaking it into \
separate, focused questions that each have a single atomic answer.
- **Try a reversal**: if the forward question is ambiguous, suggest reversing it \
(e.g. instead of "What does X do?" try "What command/term does Y?").
- **Narrow the scope**: if the expected answer is one of several valid responses, suggest \
constraining the question to eliminate alternatives (e.g. "In Linux, ..." or "Using Git, ...").

Respond ONLY with a JSON object in this exact format — no additional text:
{
    "results": [
        {"question_index": 0, "passed": true, "feedback": "Clear and unambiguous."},
        {"question_index": 1, "passed": false, "feedback": "The question could refer to X or Y. Suggest: ..."}
    ]
}"""

SCORER_SYSTEM_PROMPT = """\
You are a flashcard review scorer. You will receive flashcards that a user has answered, each with:
- The question text
- The expected answer
- The user's answer
- Time spent (seconds the user spent looking at the question before revealing the answer)
- Optional testing notes describing how to assess responses

Your job is to score how well the user's answer matches the expected answer.

Scoring scale (1-4):
- 1 (again): The answer is wrong, missing, or shows no understanding. The user needs to review this card again.
- 2 (hard): The answer shows some understanding but has significant gaps or errors. The user struggled.
- 3 (good): The answer is correct or mostly correct. Solid recall with only minor omissions.
- 4 (easy): The answer is excellent — correct, complete, and confident. Effortless recall.

Guidelines:
- Focus on whether the user demonstrates understanding of the core concept, not verbatim recitation.
- Minor wording differences, synonyms, or different phrasing of the same idea should not reduce the score.
- If testing notes are provided, use them to guide your assessment.
- For coding questions, consider whether the answer would work in practice — correct logic with minor \
syntax issues is a 3-4, while incorrect logic with correct syntax is a 1-2.
- Use the time spent as a signal for confidence: a correct answer given quickly suggests easy recall (4), \
while a correct answer after a long pause suggests the user had to work harder (2-3). Time alone should \
never override answer quality — a wrong answer is still 1 regardless of speed.
- Keep feedback brief and constructive — one sentence explaining the score.

Respond ONLY with a JSON object in this exact format — no additional text:
{
    "results": [
        {"flashcard_id": 1, "score": 3, "feedback": "Good — identified the key concept."},
        {"flashcard_id": 2, "score": 2, "feedback": "Hard — got the gist but missed X."}
    ]
}"""


def _build_structured_subagent(
    checkpointer, model, system_prompt: str,
) -> tuple[CompiledStateGraph, PromptEngine]:
    """Build a toolless, one-shot subagent that emits a strict JSON object as its final message.

    ``AgentSession`` parses that message into ``InvokeResult.structured_response`` (a dict), which the
    consuming tool/widget reads — so no ``response_format`` is needed. The base ``PromptEngine`` does only
    history repair and payload ingestion; the system prompt rides every request via ``create_agent``.
    """
    engine = PromptEngine()
    agent = create_agent(
        model=model,
        system_prompt=system_prompt,
        tools=[],
        middleware=[PromptCompilerMiddleware(engine)],
        context_schema=BaseAgentContext,
        state_schema=BaseAgentState,
        checkpointer=checkpointer,
    )
    return agent, engine


def build_flashcard_answerer(
    *,
    checkpointer: AgentCheckpointerService,
    api_keys: APIKeyService,
    provider: Annotated[str, Options.Agent.Provider],
) -> tuple[CompiledStateGraph, PromptEngine]:
    """Answerer: attempts each flashcard question cold (no context). Fast tier, temp 0 for determinism."""
    model = _make_model(provider, api_keys, _FAST_MODELS, temperature=0.0)
    return _build_structured_subagent(checkpointer, model, ANSWERER_SYSTEM_PROMPT)


def build_flashcard_comparator(
    *,
    checkpointer: AgentCheckpointerService,
    api_keys: APIKeyService,
    provider: Annotated[str, Options.Agent.Provider],
) -> tuple[CompiledStateGraph, PromptEngine]:
    """Comparator: judges whether the answerer's cold answer shows each card is unambiguous. Balanced tier
    (the judgment call is subtler than answering), temp 0."""
    model = _make_model(provider, api_keys, _BALANCED_MODELS, temperature=0.0)
    return _build_structured_subagent(checkpointer, model, COMPARATOR_SYSTEM_PROMPT)


def build_flashcard_scorer(
    *,
    checkpointer: AgentCheckpointerService,
    api_keys: APIKeyService,
    provider: Annotated[str, Options.Agent.Provider],
) -> tuple[CompiledStateGraph, PromptEngine]:
    """Scorer: rates a user's review answers against the expected answers on a 1-4 scale. Fast tier, temp 0.

    TODO(cache): the scorer is invoked repeatedly with the same rubric (system prompt) but varying per-card
    user messages, so it benefits from an Anthropic prompt-cache breakpoint on the prefix — wire that in once
    the engine's cache-breakpoint policy lands (see the TODO in ``root.build_root_agent``)."""
    model = _make_model(provider, api_keys, _FAST_MODELS, temperature=0.0)
    return _build_structured_subagent(checkpointer, model, SCORER_SYSTEM_PROMPT)


# ========================================================================================================================
# COMMIT SUBAGENT
# ========================================================================================================================


class CommitSubagentState(BaseAgentState):
    """The commit subagent's graph state — mirrors the root's proposal entries so the subagent operates on
    the same shape. ``tools.commit.commit_invoke_subagent`` seeds ``commit_proposal`` via a
    ``StateUpdatePayload`` and reads it back off the run's final state."""

    commit_proposal: list[CommitProposalEntry]


@dataclass
class CommitSubagentContext(BaseAgentContext):
    """Context for the commit subagent: just the DB session factory its read tools open sessions on (injected
    by ``AgentRuntime.new`` — note the bare service annotation). The framework ``pending`` / ``runtime``
    fields come from ``BaseAgentContext``."""

    session_factory: SessionFactoryService = None


COMMIT_SYSTEM_PROMPT = """\
You are a knowledge extraction assistant for a knowledge management system.

Given a set of conversation messages from a learning session, your task is to propose structured knowledge
entries to commit to the database. Each entry should capture a discrete, self-contained piece of knowledge
from the conversation.

You have read-only database tools (`query`, `aggregate`) to inspect existing topics and entries so you can:
- Determine which topic_id to assign each entry to
- Avoid creating duplicate entries
- Understand the existing knowledge structure

""" + KNOWLEDGE_ENTRIES_GUIDE + """

## How to propose entries

Use the `stage_entries` tool to create your initial proposal. Each entry needs:
- `title`: short descriptive title
- `content`: full content of the knowledge entry
- `entry_type`: one of "fact", "exposition", or "overview"
- `topic_id`: integer topic ID (use the `query` tool to find the right one)

If you are revising an existing proposal (e.g. after user feedback), use `edit_entries` to make targeted
changes by stable ID. Do NOT use `stage_entries` to replace the entire proposal — that would discard user
edits.

Once you have staged or edited entries, respond with a brief summary of what you proposed or changed. Do NOT
include the full entry content in your response.
"""


def _build_commit_subagent_tools() -> list:
    """The commit subagent's private proposal tools — NOT exposed to the root agent. They read and write the
    subagent's own ``commit_proposal`` state field via ``Command(update=...)``."""

    @tool("stage_entries", description=(
        "Stage a fresh set of knowledge entries as the commit proposal. "
        "Use this for initial proposal creation. Each entry is auto-assigned "
        "a stable ID. Do NOT use this to revise an existing proposal — use "
        "edit_entries instead to preserve user edits."
    ))
    async def stage_entries(entries: list[KnowledgeEntryProposalSchema], runtime: ToolRuntime) -> Command:
        if not entries:
            return Command(update={
                "messages": [ToolMessage(content="Error: no entries provided.", tool_call_id=runtime.tool_call_id)],
            })

        proposal = [
            CommitProposalEntry(
                id=i, title=e.title, content=e.content, entry_type=e.entry_type, topic_id=e.topic_id,
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


def build_commit_subagent(
    *,
    checkpointer: AgentCheckpointerService,
    api_keys: APIKeyService,
    provider: Annotated[str, Options.Agent.Provider],
) -> tuple[CompiledStateGraph, PromptEngine]:
    """Build the commit subagent and its prompt engine, returning ``(agent, engine)``.

    Read-only DB access (``query`` / ``aggregate``) lets it pick topic ids and avoid duplicates; the actual
    write happens later, after the user approves, through the root agent's ``commit_proposal_accept`` tool —
    so the subagent only ever *proposes*, via its private stage/edit tools. The schema reference is appended
    to the prompt (as it is for the root agent) so the generic DB tools are usable. Balanced tier, temp 0.1.
    """
    model = _make_model(provider, api_keys, _BALANCED_MODELS, temperature=0.1)

    db_tools = build_database_tools()
    tools = [db_tools["query"], db_tools["aggregate"], *_build_commit_subagent_tools()]

    system_prompt = f"{COMMIT_SYSTEM_PROMPT}\n\n{render_schema_reference()}"

    engine = PromptEngine()
    agent = create_agent(
        model=model,
        system_prompt=system_prompt,
        tools=tools,
        middleware=[PromptCompilerMiddleware(engine)],
        context_schema=CommitSubagentContext,
        state_schema=CommitSubagentState,
        checkpointer=checkpointer,
    )
    return agent, engine


# ========================================================================================================================
# REGISTRATION
# ========================================================================================================================


def register_subagents(factory: AgentFactory) -> None:
    """Register every subagent kind on *factory*. Called from ``root.build_agent_factory`` at app
    composition, so the per-workspace ``AgentRuntime`` can mint these by key off one populated registry. The
    keys match the constants the consuming tools look up."""
    factory.register(
        COMMIT_SUBAGENT_KEY, build=build_commit_subagent,
        context_schema=CommitSubagentContext, state_schema=CommitSubagentState,
    )
    factory.register(
        ANSWERER_KEY, build=build_flashcard_answerer,
        context_schema=BaseAgentContext, state_schema=BaseAgentState,
    )
    factory.register(
        COMPARATOR_KEY, build=build_flashcard_comparator,
        context_schema=BaseAgentContext, state_schema=BaseAgentState,
    )
    factory.register(
        FLASHCARD_SCORER_KEY, build=build_flashcard_scorer,
        context_schema=BaseAgentContext, state_schema=BaseAgentState,
    )
