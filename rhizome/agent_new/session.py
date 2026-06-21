"""Agent session: a conversation handle over the runtime.

An ``AgentSession`` drives one conversation thread. It owns the durable bits a thread needs — its context
instance (the live payload queue, resource stores, hooks) and its thread id — while the *agent* and
*engine* are resolved fresh from the runtime on every ``acquire``. The split buys us:

- Automatic rebuilds: provider/model changes invalidate the runtime's cached graph, the next ``acquire``
  rebuilds it, and the shared checkpointer keeps this thread's history meaningful across the swap. The
  session's context (and its queue) ride through untouched.
- Subagent unification: a persistent conversation with any registered agent is just a session the runtime
  owns; tools emit ``session.thread_id`` and resume later via ``runtime.get(key, thread_id)``.

Sessions are created and owned by the runtime — ``AgentRuntime.new`` builds the context, wires the queue
onto it, and constructs the session. Input flows as payloads, not messages: ``send()`` queues
``AgentPayload`` objects, and the agent's own ``PromptCompilerMiddleware`` ingests them at each model call
(see ``prompt_engine.py``). Payloads stage through two stops:

- the backlog — payloads posted while idle; moved into the live queue when a run starts;
- the live queue — ``context.pending``, drained by the engine each model call. ``send(..., eager=True)``
  during a run posts here directly, so the payload is consumed at the next model call of the *current*
  run — provided the run consumes live (streams do by default, invokes don't; see ``consume_live``).
"""

import asyncio
import json
from dataclasses import dataclass, replace
from typing import Any, TYPE_CHECKING

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from rhizome.logs import get_logger

# AgentPayload et al. are re-exported here: payload types are part of the session's public API (callers
# construct them for send()), even though they live in payload.py to keep the import graph acyclic.
from .payload import AgentPayload, MessagePayload, PayloadQueue, StateUpdatePayload  # noqa: F401
from .prompt_engine import PromptEngine
from .streaming import AgentStreamingContext, RunStateView

if TYPE_CHECKING:
    from .runtime import AgentRuntime

_logger = get_logger("agent.session")


@dataclass(frozen=True)
class AgentInstance:
    """A run-ready bundle for one (agent, thread): the runtime's current template parts plus this
    session's own context and config. Transient — assembled fresh by ``AgentSession.acquire`` each call,
    so an option-driven rebuild is picked up on the next run; never stored.
    """

    agent: CompiledStateGraph
    engine: PromptEngine
    context: Any
    config: RunnableConfig

    @property
    def thread_id(self) -> str:
        return self.config["configurable"]["thread_id"]


@dataclass(frozen=True)
class InvokeResult:
    """What a one-shot run produced.

    Invocation, unlike streaming, naturally *returns* rather than handling events as they arise — so
    instead of a callback context, ``invoke`` hands back the pieces tool-side callers actually consume.
    """

    thread_id: str
    """The thread this run executed on — emit it (e.g. in a tool message) to continue the conversation."""

    response: AIMessage | None
    """The final AI message of the run, or ``None`` if the run produced no AI message."""

    state: dict[str, Any]
    """Full final state values, for callers needing more than the response (proposal state, etc.)."""

    structured_response: Any | None
    """Parsed structured output, if any — see ``AgentSession._extract_structured_response``."""


class AgentSession:
    """A conversation handle: send/stream/invoke over one (agent, thread) the runtime owns.

    Construct sessions through ``AgentRuntime.new`` and fetch them through ``AgentRuntime.get`` — the
    runtime builds the context (wiring the live ``PayloadQueue`` onto ``context.pending``) before calling
    this constructor. The session reads its queue straight off the context, so the queue it posts into and
    the queue the engine drains are guaranteed to be one and the same object — no second copy to keep in
    sync.
    """

    def __init__(self, runtime: "AgentRuntime", key: Any, thread_id: str, context: Any) -> None:
        self._runtime = runtime
        self._key = key
        self._thread_id = thread_id
        self._context = context
        self._queue = context.pending   # single source of the live queue — see the class docstring
        self._config: RunnableConfig = {"configurable": {"thread_id": thread_id}}

        self._backlog: list[AgentPayload] = []
        self._busy = False
        self._consume_live = True   # per-run; set by _begin_run

    # -------------------------------------------------------------
    # Introspection
    # -------------------------------------------------------------

    @property
    def thread_id(self) -> str:
        return self._thread_id

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def queued(self) -> list[AgentPayload]:
        """Payloads waiting for the next run (excludes eager payloads already in the live queue)."""
        return list(self._backlog)

    def acquire(self) -> AgentInstance:
        """Assemble a run-ready ``AgentInstance``: the runtime's current agent + engine for this key plus
        this session's own context and config. One fresh bundle per call, so a rebuilt graph/engine is
        picked up on the next run."""
        return AgentInstance(
            self._runtime._get_agent(self._key),
            self._runtime._get_prompt_engine(self._key),
            self._context,
            self._config,
        )

    @property
    def agent(self) -> CompiledStateGraph:
        return self.acquire().agent

    @property
    def agent_context(self) -> Any:
        return self._context

    @property
    async def agent_state(self) -> dict[str, Any]:
        """This thread's current checkpointed state values (``{}`` when the thread is empty). Async —
        the only state read, matching the async checkpointer; ``stream``/``invoke`` set the precedent."""
        acq = self.acquire()
        return dict((await acq.agent.aget_state(acq.config)).values or {})

    async def seed_state(self, values: dict[str, Any]) -> None:
        """Overwrite this thread's checkpoint with ``values``, attributed as graph input.

        ``as_node="__start__"`` attributes the write as input; without it a second update on a thread
        that already has a checkpoint raises ``InvalidUpdateError("Ambiguous update")`` — langgraph
        cannot infer attribution. Intended for seeding a fresh thread (the graph's branch/merge), not
        for editing a live conversation.
        """
        acq = self.acquire()
        await acq.agent.aupdate_state(acq.config, values, as_node="__start__")

    def update_context(self, **overrides: Any) -> None:
        """Replace fields on this session's context, taking effect on the *next* run.

        Deliberately distinct from invocation (there is no ``context_override`` on stream/invoke):
        langgraph captures the context at dispatch, so a mid-run edit would not apply and could
        desynchronize the in-flight run from its checkpoint. Framework fields (``pending``, ``runtime``)
        are off-limits — rebinding the queue would orphan the engine's drain handle.
        """
        if self._busy:
            raise RuntimeError("cannot update context while a run is in flight")
        if "pending" in overrides or "runtime" in overrides:
            raise ValueError("'pending' and 'runtime' are framework-owned and cannot be overridden")
        self._context = replace(self._context, **overrides)
        self._queue = self._context.pending

    # -------------------------------------------------------------
    # Input
    # -------------------------------------------------------------

    def send(self, payload: AgentPayload, eager: bool = False) -> None:
        """Queue a payload for the agent.

        With ``eager=True`` during an active run that consumes live (see ``stream``/``invoke``), the
        payload posts straight to the live queue and is consumed at the current run's next model call;
        otherwise it waits in the backlog for the next ``stream()``/``invoke()``.
        """
        if eager and self._busy and self._consume_live:
            self._queue.post(payload)
        else:
            self._backlog.append(payload)

    # -------------------------------------------------------------
    # Runs
    # -------------------------------------------------------------

    def _merged_config(self, override: RunnableConfig | None) -> RunnableConfig:
        """Shallow-merge a caller override onto this session's config (``configurable`` one level deep).

        The session owns thread identity: an override carrying ``thread_id`` is rejected, because
        retargeting the thread would run this session's context (and its live queue) against another
        thread's checkpointed state — a silent, severe mismatch.
        """
        if not override:
            return self._config
        over_cfg = override.get("configurable", {})
        if "thread_id" in over_cfg:
            raise ValueError(
                "runnable_config_override may not set 'thread_id' — the AgentSession owns thread identity"
            )
        merged: RunnableConfig = {**self._config, **override}
        merged["configurable"] = {**self._config["configurable"], **over_cfg}
        return merged

    def _begin_run(
        self,
        payloads: list[AgentPayload] | None,
        runnable_config_override: RunnableConfig | None,
        *,
        consume_live: bool,
    ) -> tuple[AgentInstance, RunnableConfig]:
        if self._busy:
            raise RuntimeError("AgentSession already has a run in flight")
        self._consume_live = consume_live

        acq = self.acquire()
        config = self._merged_config(runnable_config_override)

        # The backlog becomes visible to the engine, followed by any payloads handed directly to this
        # run: from here on, everything reaches the agent through the live queue via the engine's
        # compile step.
        self._queue.post_all(self._backlog)
        self._backlog.clear()
        if payloads:
            self._queue.post_all(payloads)

        return acq, config

    async def stream(
        self,
        stream_context: AgentStreamingContext,
        payloads: list[AgentPayload] | None = None,
        runnable_config_override: RunnableConfig | None = None,
        consume_live: bool = True,
    ) -> None:
        """Run the agent, feeding stream events through ``stream_context``.

        ``payloads`` is a convenience for "queue these and run": they are ingested after anything
        already in the backlog, exactly as if each had been ``send()``-ed beforehand.

        The loop restarts ``astream`` for two reasons: an interrupt (resumed with the handler's
        ``Command``), or eager payloads that landed after the run's final model call (re-entered so the
        engine can ingest them before the run is considered complete).

        ``consume_live`` controls whether eager sends are admitted into this run mid-flight; pass
        ``False`` to defer everything posted during the run to the next one.

        ``runnable_config_override`` is shallow-merged onto the run config; it may not change
        ``thread_id`` (the session owns thread identity — see ``_merged_config``).
        """
        acq, config = self._begin_run(payloads, runnable_config_override, consume_live=consume_live)

        # The run's live state view: seeded from the checkpoint here, folded forward from every
        # updates event below, handed to the context's during-stream hooks. Updates from the first
        # compile (queued mode changes, etc.) reach the view before the model's output streams.
        state_view = RunStateView((await acq.agent.aget_state(config)).values or {})

        # Input is empty on purpose — payloads travel through the live queue and are folded into state
        # by the engine's compile step at the first model call.
        next_input: dict[str, Any] | Command = {"messages": []}

        self._busy = True
        try:
            while True:
                interrupted = False

                async for kind, payload in acq.agent.astream(
                    next_input,
                    config=config,
                    context=acq.context,
                    stream_mode=["updates", "messages"],
                ):
                    if kind == "updates":
                        state_view.fold(payload)

                        if payload.get("__interrupt__"):
                            interrupt_value = payload["__interrupt__"]

                            # Extract the value from the interrupt info
                            if isinstance(interrupt_value, (list, tuple)) and len(interrupt_value) > 0:
                                interrupt_value = interrupt_value[0]
                            value = getattr(interrupt_value, "value", interrupt_value)

                            # Pass to the interrupt handler; its result resumes the stream.
                            resume = await stream_context.on_interrupt(value, acq.context, state_view)
                            next_input = resume if isinstance(resume, Command) else Command(resume=resume)
                            interrupted = True
                            break

                        await stream_context.on_update(payload, state_view)

                    elif kind == "messages":
                        # TODO: usage-metadata extraction — likely an AgentStreamingContext concern now.
                        await stream_context.on_message(payload, state_view)

                if interrupted:
                    continue
                if self._queue:
                    # Eager payloads arrived after the final model call — re-enter so the engine
                    # ingests them as part of this run.
                    next_input = {"messages": []}
                    continue
                break

        except asyncio.CancelledError:
            await self._repair(acq, config, "Tool call cancelled by user.")
            await stream_context.on_cancelled()
            raise

        except Exception as exc:
            await self._repair(acq, config, f"An error occurred during the stream request: {type(exc).__name__}")
            _logger.exception("Stream error: %s", exc)
            await stream_context.on_exception(exc)
            raise

        else:
            state = await acq.agent.aget_state(config)
            structured = self._extract_structured_response(state.values)
            if structured is not None:
                await stream_context.on_structured_response(structured)

        finally:
            self._busy = False
            await stream_context.on_complete(state_view)

    async def invoke(
        self,
        payloads: list[AgentPayload] | None = None,
        runnable_config_override: RunnableConfig | None = None,
        consume_live: bool = False,
    ) -> InvokeResult:
        """One-shot run: invoke with the payloads you want, get an ``InvokeResult`` back.

        ``payloads`` are ingested after anything already in the backlog, exactly as if each had been
        ``send()``-ed beforehand — for invocation this is the natural calling shape
        (``await session.invoke([MessagePayload(...)])``).

        By default an invoke run does NOT consume eager payloads mid-run: one-shot invocations are
        typically subagent calls where a mid-flight send belongs to the *next* turn, so eager posts wait
        in the backlog. Pass ``consume_live=True`` for stream-like live delivery.

        TODO: interrupts raised during ``ainvoke`` currently have no handler — invoke is for agents
        that don't interrupt. Revisit if that stops being true.
        """
        acq, config = self._begin_run(payloads, runnable_config_override, consume_live=consume_live)

        self._busy = True
        try:
            state = await acq.agent.ainvoke({"messages": []}, config=config, context=acq.context)

        except asyncio.CancelledError:
            await self._repair(acq, config, "Tool call cancelled by user.")
            raise

        except Exception as exc:
            await self._repair(acq, config, f"An error occurred during the invoke request: {type(exc).__name__}")
            _logger.exception("Invoke error: %s", exc)
            raise

        finally:
            self._busy = False

        messages = state.get("messages", [])
        response = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)

        return InvokeResult(
            thread_id=config["configurable"]["thread_id"],
            response=response,
            state=state,
            structured_response=self._extract_structured_response(state),
        )

    # -------------------------------------------------------------
    # Structured responses
    # -------------------------------------------------------------

    @staticmethod
    def _extract_structured_response(state_values: dict[str, Any]) -> Any | None:
        """Extract a structured response from a run's final state, if one exists.

        Remark: langchain documents structured output arriving under a "structured_response" key in the
        final state [1]; in practice we have not been able to replicate this — even with
        ProviderStrategy, the structured response arrives as a JSON string in the final AIMessage's
        content. So: try the documented path first, then fall back to parsing the final message.

        [1] - https://docs.langchain.com/oss/python/langchain/structured-output

        Returns the raw parsed object (typically a dict) — instantiating an actual schema is the
        caller's concern, since the session doesn't know the agent's response format. Returns ``None``
        when nothing parses; for ordinary prose agents that is the normal case, so parse failures log
        at debug, not warning. (Corollary: a prose response that happens to be valid JSON is reported
        as structured — acceptable, since only structured-agent consumers read this field.)
        """
        structured = state_values.get("structured_response")
        if structured is not None:
            return structured

        messages = state_values.get("messages", [])
        response = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        if response is None:
            return None

        # Failsafes for the different content formats langchain hands back, ported from the original
        # StructuredSubagent.postinvoke_hook.
        content = response.content
        if isinstance(content, list):
            if not content:
                return None
            content = content[-1]

        try:
            if isinstance(content, dict):
                return json.loads(content["text"]) if content.get("type") == "text" else content
            if isinstance(content, str):
                return json.loads(content)
        except (json.JSONDecodeError, KeyError, TypeError):
            _logger.debug("Final message content is not structured output")

        return None

    # -------------------------------------------------------------
    # Repair
    # -------------------------------------------------------------

    async def _repair(self, acq: AgentInstance, config: RunnableConfig, reason: str) -> None:
        """Patch orphaned tool calls left in the checkpoint by a broken run.

        The contract being protected is Anthropic's, not langgraph's: the API rejects a conversation
        whose ``tool_use`` has no adjacent ``tool_result``; langgraph itself tolerates the tear.
        Correctness is already guaranteed by the engine — every compile begins with an idempotent
        repair pass whose patches land adjacent to the dangling tool call, because runs start with
        empty input and everything enters state through that one update. Repairing eagerly here is
        *hygiene*: it keeps the checkpoint clean for everything that reads state between runs —
        branching off this conversation, history displays, token counting, state dumps.
        """
        try:
            state = await acq.agent.aget_state(config)
            patches = acq.engine.repair(state.values.get("messages", []), reason=reason)
            if patches:
                _logger.info("Patching %d orphaned tool call(s)", len(patches))
                # as_node="__start__": input-attributed, same as the graph's state seeding — sidesteps
                # InvalidUpdateError("Ambiguous update") on checkpoints with murky attribution.
                await acq.agent.aupdate_state(config, {"messages": patches}, as_node="__start__")
        except Exception:
            _logger.exception("Failed to repair conversation history after a broken run")
