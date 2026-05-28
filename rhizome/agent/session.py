"""Agent session: owns the LangGraph agent and message queue."""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.types import Command

from rhizome.config import get_log_dir

from rhizome.agent.builder import build_root_agent
from rhizome.agent.context import AgentContext
from rhizome.agent.middleware.agent_mode import AgentModeMiddleware, SYSTEM_PROMPT_MESSAGE_ID
from rhizome.agent.modes import MODE_REGISTRY
from rhizome.agent.tools.app import build_app_tools
from rhizome.agent.tools.core import build_core_tools
from rhizome.agent.tools.flashcard_proposal import build_flashcard_proposal_tools
from rhizome.agent.tools.guide import build_guide_tools
from rhizome.agent.tools.resources import build_resource_tools
from rhizome.agent.tools.review import build_review_tools
from rhizome.agent.tools.sql import build_sql_tools
from rhizome.agent.utils import TokenUsageData, compute_chat_model_max_tokens

from rhizome.agent.subagents.commit import build_commit_subagent, build_commit_subagent_tools
from rhizome.agent.subagents.flashcard_validator import (
    build_answerer_subagent,
    build_comparator_subagent,
    build_scorer_subagent,
)

from rhizome.logs import get_logger
from rhizome.resources import ResourceManager
from rhizome.app.options import Options


def _merge_resource_messages_into_queue(
    queued: list[BaseMessage],
    resource_messages: list[BaseMessage],
) -> list[BaseMessage]:
    """Splice context-stuffing messages into ``queued`` at the right position.

    We place them immediately **before** the last ``HumanMessage`` whose
    content does **not** start with ``"[System]"`` — i.e. immediately
    before the user's current turn.  This keeps the CS content adjacent
    to the user's input while ensuring it lands after the SystemMessage
    (system prompt) on the very first turn.

    RemoveMessages and HumanMessages are treated identically here: the
    ``add_messages`` reducer handles removals by id regardless of
    position, so we just insert them at the same spot.

    Falls back to appending at the end if ``queued`` has no non-``[System]``
    HumanMessage (e.g. only system-prompt or settings-injection messages).
    """
    if not resource_messages:
        return queued

    insert_at = len(queued)
    for i in range(len(queued) - 1, -1, -1):
        msg = queued[i]
        if isinstance(msg, HumanMessage):
            content = msg.content if isinstance(msg.content, str) else ""
            if not content.startswith("[System]"):
                insert_at = i
                break

    return queued[:insert_at] + resource_messages + queued[insert_at:]


def get_agent_kwargs(options: Options) -> dict[str, Any]:
    """Build provider-specific kwargs from the current options."""
    provider = options.get(Options.Agent.Provider)

    kwargs: dict[str, Any] = {}
    kwargs["parallel_tool_calling"] = options.get(Options.Agent.ParallelToolCalling) == "enabled"
    kwargs["temperature"] = options.get(Options.Agent.Temperature)
    kwargs["answer_verbosity"] = options.get(Options.Agent.AnswerVerbosity)
    kwargs["planning_verbosity"] = options.get(Options.Agent.PlanningVerbosity)

    if provider == "anthropic":
        kwargs["prompt_cache"] = options.get(Options.Agent.Anthropic.PromptCache) == "enabled"
        kwargs["prompt_cache_ttl"] = options.get(Options.Agent.Anthropic.PromptCacheTTL)
        kwargs["web_tools"] = options.get(Options.Agent.Anthropic.WebTools) == "enabled"

    return kwargs


def _extract_middleware[T](middleware_list: list, cls: type[T]) -> T:
    """Find and return the first middleware instance of the given type."""
    for mw in middleware_list:
        if isinstance(mw, cls):
            return mw
    raise RuntimeError(f"Expected {cls.__name__} in middleware list but not found")


class AgentSession:
    """Encapsulates a single conversation's agent graph and message queue.

    Messages are queued via ``add_human_message`` / ``add_system_notification``
    and drained into the graph on each ``stream()`` call.  The graph's
    checkpointer (``InMemorySaver``) maintains the full conversation history;
    this class never passes the full history itself.
    """

    def __init__(
            self,
            session_factory,
            *,
            chat_pane=None,
            resource_manager: ResourceManager | None = None,
            provider: str = "anthropic",
            model_name: str | None = None,
            agent_kwargs: dict[str, Any] | None = None,
            on_token_usage_changed: Callable[["AgentSession"], Any] | None = None,
            on_rebuild_agent: Callable[[str, str], Any] | None = None,
            thread_id: str | None = None,
            debug: bool = False,
        ):
        self._session_factory = session_factory
        self._chat_pane = chat_pane
        self._resource_manager = resource_manager
        self._provider = provider
        self._model_name = model_name
        self._agent_kwargs = agent_kwargs or {}
        self.thread_id = thread_id or str(uuid.uuid4())
        self._debug = debug
        self._dump_dir: Path | None = None
        if debug:
            log_dir = get_log_dir()
            # Find the next sequential index for agent-stream directories.
            max_idx = 0
            for p in log_dir.glob("agent-stream-*"):
                try:
                    max_idx = max(max_idx, int(p.name.split("-")[-1]))
                except ValueError:
                    pass
            self._dump_dir = log_dir / f"agent-stream-{max_idx + 1}"
            self._dump_dir.mkdir(parents=True, exist_ok=True)

        # Flashcard subagents live on the session so they can be rebuilt if
        # options change. The answerer/comparator are passed directly into the
        # flashcard-proposal tools (tool-level invocation). The scorer is no
        # longer wired into review tools — FlashcardReview pulls it off
        # AgentContext at interrupt time and runs scoring inside the widget.
        self._answerer_subagent = build_answerer_subagent(**dict(self._agent_kwargs))
        self._comparator_subagent = build_comparator_subagent(**dict(self._agent_kwargs))
        self._scorer_subagent = build_scorer_subagent(**dict(self._agent_kwargs))

        # Build all tool groups (each closed over session_factory and/or chat_pane).
        self._tools: list = [
            *build_core_tools(session_factory).values(),
            *build_app_tools(session_factory, chat_pane).values(),
            *build_review_tools(session_factory).values(),
            *build_flashcard_proposal_tools(
                session_factory, self._answerer_subagent, self._comparator_subagent
            ).values(),
            *build_sql_tools(session_factory).values(),
            *build_guide_tools().values(),
            *build_resource_tools(session_factory, self._resource_manager).values(),
        ]

        # Build the commit subagent and add its tools to the root agent's tool list.
        self._commit_subagent = build_commit_subagent(
            session_factory, chat_pane, **dict(self._agent_kwargs)
        )
        self._tools.extend(
            build_commit_subagent_tools(session_factory, chat_pane, self._commit_subagent)
        )

        self._model, self._agent, middleware = build_root_agent(
            self._tools, self._provider, self._model_name,
            debug=debug,
            **self._agent_kwargs,
        )
        self._mode_middleware = _extract_middleware(middleware, AgentModeMiddleware)

        self._session_logger = get_logger("agent.session")
        self._session_logger.info("Session created (provider=%s, model=%s)", provider, model_name)

        # Message queue: messages added here are drained into the graph on the
        # next stream() call.  The system prompt is seeded here so it appears
        # in the graph state (for debugging / log dumps).  AgentModeMiddleware
        # keeps it in sync with the active mode on every model call.
        idle_mode = MODE_REGISTRY["idle"](debug=debug)
        self._message_queue: list[BaseMessage] = [
            SystemMessage(content=idle_mode.system_prompt, id=SYSTEM_PROMPT_MESSAGE_ID)
        ]

        self._token_usage = TokenUsageData()
        self._token_usage.max_tokens = compute_chat_model_max_tokens(self._model)
        self.on_token_usage_changed = on_token_usage_changed
        self.on_rebuild_agent = on_rebuild_agent

        # User settings injection — persistent messages queued when settings change.
        self._last_injected_settings: dict[str, Any] | None = None

        # Pending commit payload — set by the TUI before starting a commit stream.
        self._pending_commit_payload: list[dict] | None = None


    def rebuild_agent(self, provider: str, model_name: str, agent_kwargs: dict[str, Any] | None = None) -> None:
        """Rebuild the agent graph with the given provider and model.

        The previous graph's message history is preserved by draining it
        into the message queue so the next ``stream()`` call seeds the
        new graph with the full conversation.
        """
        old_model = self._model_name or "(default)"
        self._session_logger.info("Agent rebuilt: %s → %s", old_model, model_name)

        # Snapshot the full conversation from the old graph and prepend it
        # to the message queue (ahead of any already-queued messages).
        prior_messages = self._get_graph_messages()
        if prior_messages:
            pending = self._message_queue
            self._message_queue = prior_messages + pending

        self._provider = provider
        self._model_name = model_name
        if agent_kwargs is not None:
            self._agent_kwargs = agent_kwargs
        self._model, self._agent, middleware = build_root_agent(
            self._tools, provider, model_name,
            debug=self._debug,
            **self._agent_kwargs,
        )
        self._mode_middleware = _extract_middleware(middleware, AgentModeMiddleware)
        self._token_usage.max_tokens = compute_chat_model_max_tokens(self._model)
        if self.on_rebuild_agent is not None:
            self.on_rebuild_agent(old_model, model_name)

    def fork(self) -> "AgentSession":
        """Build a fresh session seeded with this one's full message history.

        Use case: a conversation branch wants to start from this session's exact point in time but
        proceed independently. The fork inherits the same configuration (model, provider,
        agent_kwargs, callbacks, debug) but gets its own ``thread_id`` so its checkpointer slot
        is independent — no shared graph state, no shared message queue, no shared subagents.

        Seeding is the same snapshot/reseed mechanic ``rebuild_agent`` uses internally: the parent's
        ``_get_graph_messages()`` plus anything still pending in its outbound queue are prepended to
        the new session's queue. The fork's own ``SystemMessage`` (added at construction with
        ``SYSTEM_PROMPT_MESSAGE_ID``) sits at the tail of the prepended block; on the new session's
        first ``stream()`` drain, ``add_messages`` dedupes by id so the fork's system prompt is the
        one that ends up active in the new graph — content carries forward, identity stays fresh.

        Including the queue in the snapshot matters: messages already queued on the parent but not
        yet drained (e.g. a fan-out from a user mode change) would otherwise be lost in the fork.
        The fork sees the same context the parent would see on *its* next stream.
        """
        new_session = AgentSession(
            self._session_factory,
            chat_pane=self._chat_pane,
            resource_manager=self._resource_manager,
            provider=self._provider,
            model_name=self._model_name,
            agent_kwargs=dict(self._agent_kwargs),
            on_token_usage_changed=self.on_token_usage_changed,
            on_rebuild_agent=self.on_rebuild_agent,
            debug=self._debug,
        )
        seed = self._get_graph_messages() + list(self._message_queue)
        if seed:
            new_session._message_queue = seed + new_session._message_queue
        return new_session

    async def on_options_post_update(self, options: Options) -> None:
        """Called by Options.post_update(); rebuilds agent if provider/model/kwargs changed."""
        provider = options.get(Options.Agent.Provider)
        model_name = options.get(Options.Agent.Model)
        new_kwargs = get_agent_kwargs(options)

        if provider != self._provider or model_name != self._model_name or new_kwargs != self._agent_kwargs:
            self.rebuild_agent(provider, model_name, agent_kwargs=new_kwargs)

    def set_commit_payload(self, payload: list[dict]) -> None:
        """Store a commit payload to be injected into state on the next stream() call."""
        self._pending_commit_payload = payload

    async def set_pending_user_mode(self, mode_name: str) -> None:
        """Queue a user-initiated mode change to be applied on the next model call.

        Called by ``ChatPane._set_mode()`` for user-initiated mode changes
        (shift+tab, slash commands).  The pending mode is consumed by
        ``AgentModeMiddleware.abefore_model`` which updates graph state and
        injects a notification message.

        Agent-initiated mode changes (the ``set_mode`` tool) do NOT go
        through this path — they update graph state directly via
        ``Command(update={"mode": ...})``.
        """
        await self._mode_middleware.set_pending_user_mode(mode_name)

    def add_human_message(self, text: str) -> None:
        self._message_queue.append(HumanMessage(content=text))

    def add_ai_message(self, text: str) -> None:
        """Queue a synthetic AI message into the conversation history."""
        self._message_queue.append(AIMessage(content=text))

    def add_system_notification(self, text: str) -> None:
        # Remark: certain providers only allow a single SystemPrompt at the beginning of the conversation, so we represent these
        # as human messages with a [System] prefix.
        self._message_queue.append(HumanMessage(content=f"[System] {text}"))

    def _drain_queue(self) -> list[BaseMessage]:
        """Return all queued messages and clear the queue."""
        messages = list(self._message_queue)
        self._message_queue.clear()
        return messages

    def _get_graph_messages(self) -> list[BaseMessage]:
        """Read the full message history from the graph's checkpointed state."""
        config = {"configurable": {"thread_id": self.thread_id}}
        try:
            state = self._agent.get_state(config)
            return list(state.values.get("messages", []))
        except Exception:
            return []

    async def stream(
        self,
        *,
        mode: str = "idle",
        topic_name: str = "",
        on_message: Callable[[str, Any], Awaitable[None]] | None = None,
        on_update: Callable[[str, Any], Awaitable[None]] | None = None,
        on_interrupt: Callable[[Any, AgentContext], Awaitable[Any]] | None = None,
        post_chunk_handler: Callable[[], Any] | None = None,
        cursor: Any = None,
    ) -> None:
        """Stream agent output using callbacks, with interrupt/resume support.

        Token usage is tracked automatically: ``total_tokens`` is updated from
        ``usage_metadata`` on message chunks, and ``overhead_tokens`` is computed
        after the stream completes.  The ``on_token_usage_changed`` callback fires
        whenever these values change.

        Callbacks:
            on_message(kind, payload) — called for each ``"messages"`` chunk
            on_update(kind, payload) — called for each ``"updates"`` chunk
            on_interrupt(interrupt_value, context) — called when the graph interrupts;
                must return the resume value to continue the graph
            post_chunk_handler() — called after every chunk (e.g. for scrolling)
        """
        self._session_logger.debug("Stream started (mode=%s, topic=%s)", mode, topic_name)
        config = {"configurable": {"thread_id": self.thread_id}}

        # Drain queued messages — only these (not the full history) are sent to
        # the graph.  The checkpointer restores previous state and the
        # add_messages reducer appends these new messages.
        queued = self._drain_queue()

        # Consume resource state changes since the last stream(): the manager
        # returns HumanMessages for new or replaced context-stuffed content
        # and RemoveMessages for resources that lost all CS entries.  Splice
        # them in just before the user's current turn (see the helper for
        # the exact rule and the first-turn SystemMessage ordering).
        if self._resource_manager is not None:
            resource_messages = await self._resource_manager.consume()
            queued = _merge_resource_messages_into_queue(queued, resource_messages)

        # Build the initial state input.  Only include state fields when we
        # actually have new values — omitted keys are left untouched in the
        # checkpoint, so nullable state (commit_proposal_state,
        # flashcard_proposal_state, review, etc.) persists until explicitly
        # cleared by a tool.
        next_input: dict | Command = {"messages": queued, "mode": mode}

        # Drain pending commit payload (set by ChatPane.confirm_commit_selection).
        if self._pending_commit_payload is not None:
            from rhizome.agent.state import CommitProposalState
            next_input["commit_proposal_state"] = CommitProposalState(
                payload=self._pending_commit_payload,
                proposal=[],
                proposal_diff=None,
            )
            self._pending_commit_payload = None

        # Reset any pending user mode changes from the last invocation of .stream(). The graph state is provided
        # with the mode fresh at every invocation of .stream(), and the chat pane mode always takes priority. The
        # pending user mode changes are just so that user-initiated mode changes can be propagated to the agent
        # state _during_ execution (before the next invocation of the model).
        await self._mode_middleware.clear_pending_user_mode()

        try:
            user_settings = {
                "answer_verbosity": self._agent_kwargs.get("answer_verbosity", "auto"),
                "planning_verbosity": self._agent_kwargs.get("planning_verbosity", "low"),
            }

            # Inject a persistent settings message when settings change.
            if user_settings != self._last_injected_settings:
                if user_settings:
                    payload = json.dumps(user_settings, indent=2)
                    queued.append(HumanMessage(
                        content=f"[System] Respond with these user settings:\n```json\n{payload}\n```"
                    ))
                self._last_injected_settings = dict(user_settings)

            context = AgentContext(
                user_settings=user_settings,
                conversation_cursor=cursor,
                answerer_subagent=self._answerer_subagent,
                comparator_subagent=self._comparator_subagent,
                scorer_subagent=self._scorer_subagent,
                commit_subagent=self._commit_subagent,
                session_factory=self._session_factory,
            )

            while True:
                interrupted = False

                async for update in self._agent.astream(
                    next_input,
                    config=config,
                    context=context,
                    stream_mode=["updates", "messages"],
                ):
                    kind, payload = update

                    if kind == "updates":

                        # Check for interrupt
                        if (
                            on_interrupt and \
                            "__interrupt__" in payload and \
                            payload["__interrupt__"]
                        ):
                            interrupt_value = payload["__interrupt__"]

                            # Extract the value from the interrupt info
                            if isinstance(interrupt_value, (list, tuple)) and len(interrupt_value) > 0:
                                interrupt_value = interrupt_value[0]
                            value = getattr(interrupt_value, "value", interrupt_value)

                            # Pass to interrupt handler
                            resume = await on_interrupt(value, context)

                            # Construct the Command break, restarting the stream with
                            # Command(resume) as the next input.
                            if isinstance(resume, Command):
                                next_input = resume
                            else:
                                next_input = Command(resume=resume)
                            interrupted = True
                            break

                        # Pass to update handler
                        if on_update:
                            await on_update(kind, payload)

                    elif kind == "messages":
                        chunk, _metadata = payload

                        # Extract token/cache usage metadata and notify a
                        # token usage update.
                        self._extract_usage_metadata(chunk)

                        # Pass to message handler
                        if on_message:
                            await on_message(kind, payload)

                    if post_chunk_handler:
                        result = post_chunk_handler()
                        if result is not None and hasattr(result, "__await__"):
                            await result

                if not interrupted:
                    # astream completed without interrupt → done
                    break
                # otherwise loop continues with Command(resume=...) as next_input

        except asyncio.CancelledError:
            self._patch_orphaned_tool_calls("Tool call cancelled by user.")
            raise
        except Exception as exc:
            self._patch_orphaned_tool_calls(
                f"An error has occurred during the stream request: {type(exc).__name__}"
            )
            self._session_logger.exception("Stream error: %s", exc, exc_info=exc, stack_info=True)
            raise
        else:
            self._session_logger.debug(
                f"Stream complete (tokens={self._token_usage.total_tokens}, "
                f"cache_read={self._token_usage.cache_read_tokens}, "
                f"cache_create={self._token_usage.cache_creation_tokens})"
            )
        finally:
            self._notify_token_usage()
            self._dump_graph_state()

    def _extract_usage_metadata(self, chunk):
        if not (hasattr(chunk, "usage_metadata") and chunk.usage_metadata):
            return

        if chunk.usage_metadata.get("total_tokens"):
            self._token_usage.total_tokens = chunk.usage_metadata["total_tokens"]

        details = chunk.usage_metadata.get("input_token_details", {})
        cache_read = details.get("cache_read")
        cache_create = details.get("cache_creation")

        if not cache_read and not cache_create:
            resp_meta = getattr(chunk, "response_metadata", {})
            usage = resp_meta.get("usage", {})
            cache_read = usage.get("cache_read_input_tokens")
            cache_create = usage.get("cache_creation_input_tokens")

        if cache_read or cache_create:
            self._token_usage.cache_read_tokens = cache_read
            self._token_usage.cache_creation_tokens = cache_create

        self._notify_token_usage()

    def _dump_graph_state(self) -> None:
        """Dump the full graph message state to a timestamped JSON file."""
        if self._dump_dir is None:
            return
        try:
            messages = self._get_graph_messages()
            parts: list[str] = []
            for i, msg in enumerate(messages):
                if hasattr(msg, "model_dump"):
                    body = json.dumps(msg.model_dump(), indent=2, default=str)
                elif hasattr(msg, "dict"):
                    body = json.dumps(msg.dict(), indent=2, default=str)
                else:
                    body = repr(msg)
                header = f"{'=' * 60}\n  [{i}] {type(msg).__name__}\n{'=' * 60}"
                parts.append(f"{header}\n{type(msg).__name__}({body})")
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%f")
            path = self._dump_dir / f"{ts}.txt"
            path.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
            self._session_logger.debug("Graph state dumped to %s", path)
        except Exception as exc:
            self._session_logger.warning("Failed to dump graph state: %s", exc)

    def _patch_orphaned_tool_calls(self, message: str) -> None:
        """Inject synthetic ToolMessages for any tool_use blocks without results.

        When a stream is interrupted mid-tool-call, the AIMessage with
        ``tool_use`` content may already be in the graph state but the
        corresponding ``ToolMessage`` was never appended.  The Anthropic
        API rejects conversations where a ``tool_use`` has no matching
        ``tool_result``, so we scan the graph state and queue patches.
        """
        graph_messages = self._get_graph_messages()

        # Collect tool_call IDs that already have a ToolMessage.
        answered: set[str] = set()
        for msg in graph_messages:
            if isinstance(msg, ToolMessage) and msg.tool_call_id:
                answered.add(msg.tool_call_id)

        # Walk backwards to find the most recent AIMessage with tool calls.
        orphaned_ids: list[str] = []
        for msg in reversed(graph_messages):
            if isinstance(msg, AIMessage) and msg.tool_calls:
                for tc in msg.tool_calls:
                    if tc["id"] not in answered:
                        orphaned_ids.append(tc["id"])
                break  # only patch the most recent AIMessage

        if not orphaned_ids:
            return

        self._session_logger.info(
            "Patching %d orphaned tool call(s): %s",
            len(orphaned_ids), orphaned_ids,
        )
        for tc_id in orphaned_ids:
            self._message_queue.append(ToolMessage(
                content=message,
                tool_call_id=tc_id,
            ))

    def _notify_token_usage(self) -> None:
        self._compute_overhead_tokens()
        if self.on_token_usage_changed is not None:
            # Callback receives the firing session so the consumer (chat-pane VM) can route only
            # the updates that came from the *currently-displayed* branch to the status bar —
            # concurrent turns on background branches would otherwise overwrite the visible
            # token count.
            self.on_token_usage_changed(self)

    def _compute_overhead_tokens(self) -> None:
        """Estimate overhead tokens (system prompt + tool messages) from graph state."""
        graph_messages = self._get_graph_messages()

        system_msgs = [m for m in graph_messages if self._is_system_message(m)]
        tool_msgs = [m for m in graph_messages if self._is_tool_message(m)]

        system_overhead = count_tokens_approximately(system_msgs)
        tool_overhead = count_tokens_approximately(tool_msgs)

        self._token_usage.breakdown[TokenUsageData.BreakdownCategory.SYSTEM] = system_overhead
        self._token_usage.breakdown[TokenUsageData.BreakdownCategory.TOOL_MESSAGES] = tool_overhead

    def _is_system_message(self, msg) -> bool:
        if isinstance(msg, SystemMessage):
            return True

        if isinstance(msg, HumanMessage):
            content = msg.content
            if isinstance(content, str):
                if content.startswith("[System]"):
                    return True
            elif isinstance(content, (list, tuple)):
                if len(content) != 1:
                    # TODO: might need to refactor the way we grab system messages for token counts
                    # to account for this?
                    return False
                content = content[0]
                if isinstance(content, str) and content.startswith("[System]"):
                    return True
                if (
                    isinstance(content, dict) and
                    content.get("type") == "text" and
                    content.get("text", "").startswith("[System]")
                ):
                    return True

        return False

    def _is_tool_message(self, msg) -> bool:
        return isinstance(msg, ToolMessage)

    @property
    def model(self):
        return self._model

    @property
    def message_history(self) -> list[BaseMessage]:
        """Full conversation history from the graph's checkpointed state."""
        return self._get_graph_messages()

    @property
    def token_usage(self) -> TokenUsageData:
        return self._token_usage

