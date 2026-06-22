"""Subagent: lightweight agent instances that run in isolated context windows."""

import json
import uuid
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.graph.state import CompiledStateGraph

from rhizome.logs import get_logger

_logger = get_logger("agent.subagent")


@dataclass
class Subagent:
    """A lightweight agent with its own conversation history.

    Unlike ``AgentSession``, a ``Subagent`` does not track token usage,
    manage TUI callbacks, or subscribe to options changes.  It simply
    wraps a compiled LangGraph agent and maintains a message history
    that is fully isolated from the parent session.

    Parameters
    ----------
    model:
        The underlying chat model (kept for utility access).
    agent:
        The compiled LangGraph state graph with tools bound.
    system_prompt:
        Injected as the first ``SystemMessage`` in every conversation.
    stateful:
        If ``True``, conversation history persists across ``ainvoke``
        calls sharing the same ``conversation_id``.  If ``False``,
        each call starts fresh.
    config:
        Optional ``RunnableConfig`` passed to ``agent.ainvoke()``.
    """

    model: Any  # BaseChatModel
    agent: CompiledStateGraph
    system_prompt: str
    stateful: bool = True
    config: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self._history: list[BaseMessage] = [SystemMessage(content=self.system_prompt)]
        self._conversation_id: str | None = None

    @property
    def history(self) -> list[BaseMessage]:
        return self._history

    @property
    def conversation_id(self) -> str | None:
        return self._conversation_id

    def preinvoke_hook(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        """Override to transform messages before invocation."""
        return messages

    def postinvoke_hook(self, response: AIMessage) -> AIMessage:
        """Override to transform the response after invocation."""
        return response

    async def ainvoke(
        self,
        input: str,
        conversation_id: str | None = None,
        extra_state: dict[str, Any] | None = None,
    ) -> tuple[str | None, AIMessage, dict[str, Any]]:
        """Invoke the subagent with a human message.

        Returns a ``(conversation_id, ai_message, state)`` tuple.  The
        caller should pass the returned ``conversation_id`` back on
        subsequent calls to continue the same conversation.

        Parameters
        ----------
        extra_state:
            Additional state fields to pass to the graph alongside
            messages.  Used by subagents with custom state schemas
            (e.g. the commit subagent passes ``commit_proposal``).
        """
        if (
            conversation_id is None
            or conversation_id != self._conversation_id
            or not self.stateful
        ):
            self._reset_conversation(conversation_id)

        messages = self._history + [HumanMessage(content=input)]
        if self.stateful:
            self._history = messages

        messages = self.preinvoke_hook(messages)

        graph_input: dict[str, Any] = {"messages": messages}
        if extra_state:
            graph_input.update(extra_state)

        _logger.debug("Invoking subagent with messages:\n\n%s", "\n\n".join(m.content for m in messages))

        # The agent graph is compiled with an InMemorySaver checkpointer that
        # requires a thread_id in ``configurable``. For stateful subagents we
        # reuse the conversation_id; for stateless ones we generate a fresh
        # id per invocation so the checkpointer never cross-contaminates
        # between calls.
        config: dict[str, Any] = dict(self.config or {})
        configurable = dict(config.get("configurable") or {})
        configurable.setdefault(
            "thread_id", self._conversation_id or str(uuid.uuid4())
        )
        config["configurable"] = configurable

        response = await self.agent.ainvoke(graph_input, config=config)

        ai_message = response["messages"][-1]
        ai_message = self.postinvoke_hook(ai_message)

        if self.stateful:
            self._history.append(ai_message)

        # Return all non-message state fields
        result_state = {k: v for k, v in response.items() if k != "messages"}

        return self._conversation_id, ai_message, result_state

    def _reset_conversation(self, conversation_id: str | None = None) -> None:
        self._history = [SystemMessage(content=self.system_prompt)]
        if not self.stateful:
            self._conversation_id = None
            return
        self._conversation_id = conversation_id or str(uuid.uuid4())


# Type alias — any callable that accepts **kwargs (dataclass, Pydantic model, etc.)
ResponseSchema = type


@dataclass
class StructuredSubagent(Subagent):
    """A subagent that parses structured output from the agent's response.

    The agent is expected to return JSON in its final message content.
    ``postinvoke_hook`` parses this JSON and instantiates ``response_schema``
    with the parsed data, storing the result in the ``response`` property.

    Parameters
    ----------
    response_schema:
        A callable (dataclass, Pydantic model, etc.) that accepts ``**kwargs``
        from the parsed JSON.  Required — raises ``ValueError`` if ``None``.
    """

    response_schema: ResponseSchema | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.response_schema is None:
            raise ValueError("StructuredSubagent requires a response_schema")
        self._structured_response: Any = None

    @property
    def structured_response(self) -> Any:
        """The most recent parsed structured response, or ``None`` if parsing failed."""
        return self._structured_response

    def postinvoke_hook(self, response: AIMessage) -> AIMessage:
        if self.response_schema is not None:
            try:
                # Remark: As of writing (2026-03-04) the langchain documentation says we're supposed to be able to
                # retrieve the structured output from a "structured_response" key [1], however I haven't been able to
                # replicate this. Instead, it seems like the structured response, even with ProviderStrategy, is still
                # returned as just a string in the content field, making it potentially flimsy to decode.
                #
                # [1] - https://docs.langchain.com/oss/python/langchain/structured-output
                #
                # For now, just make sure to include a strict message about the return format in the system prompt.

                # Some more failsafes for different return formats, thanks LangChain...
                content = response.content
                if isinstance(content, list):
                    content = content[-1]
                
                if isinstance(content, dict):
                    if content.get("type") == "text":
                        parsed = json.loads(content["text"])
                    else:
                        parsed = content
                elif isinstance(content, str):
                    parsed = json.loads(content)
                else:
                    raise ValueError(f"Unexpected response content type: {type(content)}")
                self._structured_response = self.response_schema(**parsed)
            except Exception as e:
                _logger.warning("Failed to parse structured response: %s", e)
                _logger.warning(
                    "Full response:\n\n" +
                    response.model_dump_json(indent=2)
                )
                self._structured_response = None
        return response
