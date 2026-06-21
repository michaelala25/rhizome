"""Streaming context: the callback surface a run drives, and the state view it feeds them.

``AgentStreamingContext`` is the per-run hook object handed to ``AgentSession.stream``. Everything a
consumer needs *during* a run arrives through these callbacks — stream events, interrupts, lifecycle
moments — so consumers never reach around the session to inspect threads mid-flight.

That includes agent state: the during-stream hooks receive a ``RunStateView``, seeded from the
checkpoint at run start and folded forward from every state update the run emits. Because payload
ingestion (mode changes, etc.) happens at the run's first model call, the view reflects those
updates *before* the model's output streams — reading the checkpoint up front would not.
"""

from typing import Any
from types import TracebackType


class RunStateView:
    """A live, run-scoped view of agent state values.

    The same instance is passed to every callback of a run: the session folds each ``updates``
    event into it before dispatching, so a callback always sees state as of the event it is
    handling. It is a *view*, not the checkpoint:

    - Last-write-wins channels (``mode``, ``verbosity``, ...) are exact.
    - Reducer-backed channels reflect the latest raw update, not reducer output.
    - ``messages`` is excluded entirely — read the checkpoint when history matters.
    """

    def __init__(self, values: dict[str, Any]) -> None:
        self._values = dict(values)
        self._values.pop("messages", None)

    def fold(self, updates: dict[str, Any]) -> None:
        """Fold one ``updates``-mode event (a node-name → update-dict mapping) into the view."""
        for update in updates.values():
            if not isinstance(update, dict):
                continue
            for key, value in update.items():
                if key != "messages":
                    self._values[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __contains__(self, key: object) -> bool:
        return key in self._values


class AgentStreamingContext:
    # Handlers - feed stream responses back to the consumer (e.g. a chat area's stream router)
    async def on_message(self, payload: Any, state: RunStateView) -> None:
        pass
    async def on_update(self, payload: Any, state: RunStateView) -> None:
        pass
    async def on_interrupt(self, interrupt_payload: Any, agent_context: Any, state: RunStateView) -> Any:
        pass

    async def on_cancelled(self) -> None:
        pass
    async def on_exception(self, exc: BaseException) -> None:
        pass

    async def on_structured_response(self, response: Any) -> None:
        pass

    async def on_complete(self, state: RunStateView) -> None:
        # Contract: fires exactly once per run — success, cancellation, or error — from the run's
        # finally block. The one teardown point a consumer can rely on unconditionally.
        pass

    def __enter__(self):
        pass
    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None):
        pass
