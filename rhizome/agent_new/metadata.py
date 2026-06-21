"""Per-message metadata: the tags the prompt engine reads to lay out — and, later, reclaim — messages.

Tags live under a single namespaced key in ``BaseMessage.additional_kwargs``, which buys three things at
once: they ride through the checkpoint (``additional_kwargs`` is part of message serialization), they
survive a branch copy with the message, and they stay off the wire (provider converters forward content
and known fields, not unknown ``additional_kwargs`` keys).

``MessageMetadata`` is the declarative schema — the one place that says what a message can carry. It is a
``TypedDict`` on purpose: at runtime it simply *is* the plain dict stored under ``additional_kwargs[META_KEY]``
(JSON-native, so it round-trips through any checkpointer and a branch copy with no special handling — a
dataclass *instance* comes back from the serializer as a bare dict and breaks ``json.dumps``). The accessor
functions below are the read/write surface: they apply per-field defaults and keep call sites off the raw
keys.

Axes today: *position* (read by ``RootPromptEngine.prepare`` to float a message to a pin anchor),
*lifetime* (a compile-born message's reclamation eligibility), the cleanup *group* / *strategy* that
steer reclamation, and *role* (a message's conversational origin, used to count genuine user turns).
"""

from typing import Literal, TypedDict

from langchain_core.messages import BaseMessage

META_KEY = "rhizome"

Position = Literal["inline", "pinned"]
"""Placement of the message in the prompt - ``inline`` messages remain in the position they arrived at
(ultimately decided by the add_messages reducer, which appends new messages with new IDs), whereas
``pinned`` messages are relocated in the ``prepare`` step according to their ``pin`` position (see below)."""

Pin = Literal["head", "branch", "tail"]
"""Named layout anchors a pinned message floats to (see the ``prompt_engine`` module docstring): ``head``
(after the system block — a graph-wide prefix), ``branch`` (this node's segment boundary), ``tail`` (the
volatile end a breakpoint sits before)."""

Lifetime = Literal["permanent", "semi-permanent"]
"""How long a compile-born message's identity persists in state (see the ``prompt_engine`` module
docstring): ``permanent`` (the default when untagged) lives forever; ``semi-permanent`` is eligible for
later reclamation."""

Strategy = Literal["stub", "stub+store", "summarize", "summarize+store"]
"""How a reclaimed message's content is replaced — two axes under one name: a *transform* (``stub`` swaps
a placeholder, ``summarize`` swaps a generated summary) and whether the original is *stored* for retrieval
(``+store``). Only ``stub`` is built today; the rest name the space. Resolved message > request > engine
default (a message may declare its own via ``set_strategy``)."""

Role = Literal["user", "agent", "system"]
"""A message's conversational origin — set on the messages a ``MessagePayload`` produces. Its job is to
tell a genuine ``user`` turn from an injected ``system`` ``HumanMessage`` (mode notices, branch markers),
which is what the reclamation expiry counter needs; ``AIMessage``/``ToolMessage`` carry their origin in
their type, so the tag is informational there."""


class MessageMetadata(TypedDict, total=False):
    """The rhizome metadata block carried in ``additional_kwargs[META_KEY]``. ``total=False`` — every
    field is optional; an absent block (or absent field) means that axis is at its default. Read through
    the accessors below, which supply those defaults."""

    position: Position
    pin: Pin
    lifetime: Lifetime
    group: str
    strategy: Strategy
    role: Role


def meta(message: BaseMessage) -> MessageMetadata:
    """A read-only view of ``message``'s metadata block — the stored dict typed as ``MessageMetadata``, or
    an empty one when untagged. Does not create the block; write through the setters below."""
    return message.additional_kwargs.get(META_KEY) or {}


# ----- position ----- #

def pin(message: BaseMessage, anchor: Pin) -> BaseMessage:
    """Tag ``message`` to float to ``anchor`` when ``prepare`` lays out the request. Mutates the
    message's metadata in place and returns it, for tagging at construction:

        pin(HumanMessage(content=block, id=mid), "head")
    """
    block = message.additional_kwargs.setdefault(META_KEY, {})
    block["position"] = "pinned"
    block["pin"] = anchor
    return message


def pin_of(message: BaseMessage) -> Pin | None:
    """The anchor ``message`` is pinned to, or ``None`` if it is inline (the default)."""
    block = meta(message)
    return block.get("pin") if block.get("position") == "pinned" else None


# ----- lifetime ----- #

def set_lifetime(message: BaseMessage, lifetime: Lifetime) -> BaseMessage:
    """Set ``message``'s lifetime tag in place; returns it for chaining at construction."""
    message.additional_kwargs.setdefault(META_KEY, {})["lifetime"] = lifetime
    return message


def lifetime_of(message: BaseMessage) -> Lifetime:
    """``message``'s lifetime, defaulting to ``permanent`` when untagged."""
    return meta(message).get("lifetime", "permanent")


# ----- cleanup group & strategy ----- #

def set_group(message: BaseMessage, group: str) -> BaseMessage:
    """Tag ``message`` with its cleanup ``group`` in place; returns it for chaining."""
    message.additional_kwargs.setdefault(META_KEY, {})["group"] = group
    return message


def group_of(message: BaseMessage) -> str | None:
    """``message``'s cleanup group, or ``None`` when untagged."""
    return meta(message).get("group")


def set_strategy(message: BaseMessage, strategy: Strategy) -> BaseMessage:
    """Tag ``message`` with a cleanup ``strategy`` in place — the highest-precedence override (message >
    request > engine). Returns it for chaining."""
    message.additional_kwargs.setdefault(META_KEY, {})["strategy"] = strategy
    return message


def strategy_of(message: BaseMessage) -> Strategy | None:
    """``message``'s self-declared cleanup strategy, or ``None`` to defer to the request/engine default."""
    return meta(message).get("strategy")


# ----- role ----- #

def set_role(message: BaseMessage, role: Role) -> BaseMessage:
    """Tag ``message`` with its conversational ``role`` in place; returns it for chaining."""
    message.additional_kwargs.setdefault(META_KEY, {})["role"] = role
    return message


def role_of(message: BaseMessage) -> Role | None:
    """``message``'s tagged role, or ``None`` when untagged."""
    return meta(message).get("role")


# ----- re-emit helper ----- #

def detached_kwargs(message: BaseMessage) -> dict:
    """An ``additional_kwargs`` copy independent at the rhizome layer (top level + the rhizome sub-dict),
    so a re-emitted copy can be re-tagged without mutating the original still living in state. Pair with
    ``model_copy(update={"additional_kwargs": detached_kwargs(msg)})``."""
    kwargs = dict(message.additional_kwargs)
    if META_KEY in kwargs:
        kwargs[META_KEY] = dict(kwargs[META_KEY])
    return kwargs
