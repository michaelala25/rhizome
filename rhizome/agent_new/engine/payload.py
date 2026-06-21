"""Payload types and the live queue connecting an ``AgentSession`` to its prompt engine.

``AgentSession.send`` accepts ``AgentPayload`` objects rather than raw messages: payloads are the session's
input language, and converting them into concrete state updates is the prompt engine's job (at ``compile``
time, inside the agent's ``before_model`` hook — see ``engine.base``).

``PayloadQueue`` is the delivery channel, and it is deliberately a *live* handle shared between a session
and its agent context (``context.pending``): the session posts into it, the engine drains it at every
model call. Payloads posted mid-run (``send(..., eager=True)`` while the session is busy) are
therefore picked up at the next model call of the *current* run; payloads posted while idle wait in the
session's backlog until the next run begins.

This module is a leaf — stdlib only. ``StateUpdatePayload`` is generic over the target state schema
``S`` rather than naming a concrete one, so the input language stays independent of any particular
agent's state (and the engine package imports nothing from ``state``).
"""

from dataclasses import dataclass
from enum import auto, Enum


@dataclass
class AgentPayload[T]:
    data: T


@dataclass
class MessagePayload(AgentPayload[str]):
    class Role(Enum):
        USER = auto()
        AGENT = auto()
        SYSTEM = auto()

    role: Role


@dataclass
class StateUpdatePayload[S](AgentPayload[S]):
    """A partial update to an arbitrary state schema ``S`` (a dict of the changed fields at runtime),
    merged into graph state through that schema's reducers."""


class PayloadQueue:
    """FIFO of payloads awaiting ingestion by a prompt engine's ``compile`` step."""

    def __init__(self) -> None:
        self._items: list[AgentPayload] = []

    def post(self, payload: AgentPayload) -> None:
        self._items.append(payload)

    def post_all(self, payloads: list[AgentPayload]) -> None:
        self._items.extend(payloads)

    def drain(self) -> list[AgentPayload]:
        items, self._items = self._items, []
        return items

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)
