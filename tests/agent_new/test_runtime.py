"""AgentRuntime: build cache, option/service injection, invalidation, session ownership.

These exercise the runtime's bookkeeping, not real graphs — builders return ``(sentinel, engine)`` pairs
so identity is easy to assert and the runtime's logic is independent of any real ``CompiledStateGraph``.
A real session-scoped ``Options`` drives injection and invalidation (its ``set`` is disk-free below Root).
No ``from __future__ import annotations``: builders carry real annotations so injection can read them.
"""

from typing import Annotated

import pytest
from langgraph.checkpoint.base import BaseCheckpointSaver

from rhizome.agent_new.checkpointer import AgentCheckpointerService
from rhizome.agent_new.context import RootAgentContext
from rhizome.agent_new.engine import PromptEngine
from rhizome.agent_new.runtime import AgentRuntimeService
from rhizome.app.options import OptionRef, Options
from rhizome.db import SessionFactoryService

from .fakes import make_runtime, register


def _trivial_build(*, checkpointer: AgentCheckpointerService):
    """Smallest valid builder: one injected service, sentinel agent + a real engine."""
    return object(), PromptEngine()


# --------------------------------------------------------------------------- #
# new() & the per-key build cache
# --------------------------------------------------------------------------- #

def test_new_produces_distinct_sessions_sharing_one_build():
    h = make_runtime()
    calls = {"n": 0}
    agent, engine = object(), PromptEngine()

    def build(*, checkpointer: AgentCheckpointerService):
        calls["n"] += 1
        return agent, engine

    register(h.runtime, "root", build)
    s1, s2 = h.runtime.new("root"), h.runtime.new("root")

    assert s1 is not s2
    assert s1.thread_id != s2.thread_id
    assert s1.thread_id.split(":")[0] == s2.thread_id.split(":")[0]   # shared workspace prefix
    assert calls["n"] == 0                                            # new() is lazy — no build yet
    # Same key -> one build, shared (agent, engine) across both sessions.
    assert s1.acquire().agent is s2.acquire().agent is agent
    assert s1.acquire().engine is s2.acquire().engine is engine
    assert calls["n"] == 1                                            # built once, then cached


def test_builder_receives_injected_services_and_option_values():
    h = make_runtime()
    seen = {}

    def build(
        *,
        checkpointer: AgentCheckpointerService,
        runtime: AgentRuntimeService,
        provider: Annotated[str, Options.Agent.Provider],
    ):
        seen.update(checkpointer=checkpointer, runtime=runtime, provider=provider)
        return object(), PromptEngine()

    register(h.runtime, "root", build)
    h.runtime.new("root").acquire()   # first acquire triggers the build

    assert isinstance(seen["checkpointer"], BaseCheckpointSaver)
    assert seen["runtime"] is h.runtime              # self-reference resolves to the live runtime
    assert seen["provider"] == "anthropic"           # injected current option value (default)


# --------------------------------------------------------------------------- #
# Invalidation
# --------------------------------------------------------------------------- #

def test_bound_option_change_rebuilds_only_for_snapshot_deps():
    h = make_runtime()
    calls = {"n": 0}

    def build(*, checkpointer: AgentCheckpointerService, provider: Annotated[str, Options.Agent.Provider]):
        calls["n"] += 1
        return object(), PromptEngine()

    register(h.runtime, "root", build)
    s = h.runtime.new("root")
    before = s.acquire().agent
    assert calls["n"] == 1

    h.options.set(Options.Agent.Provider, "openai")     # bound (snapshot) -> invalidates
    after = s.acquire().agent
    assert calls["n"] == 2 and after is not before

    h.options.set(Options.Agent.AnswerVerbosity, "verbose")   # not a dependency -> no rebuild
    s.acquire()
    assert calls["n"] == 2


def test_live_optionref_change_does_not_rebuild_but_reads_fresh():
    h = make_runtime()
    calls = {"n": 0}
    captured = {}

    def build(
        *,
        checkpointer: AgentCheckpointerService,
        provider: Annotated[str, Options.Agent.Provider],                       # snapshot (so no warning)
        ttl: Annotated[OptionRef[str], Options.Agent.Anthropic.PromptCacheTTL],  # live
    ):
        calls["n"] += 1
        captured["ttl"] = ttl
        return object(), PromptEngine()

    register(h.runtime, "root", build)
    agent = h.runtime.new("root").acquire().agent
    assert calls["n"] == 1 and captured["ttl"].get() == "5m"

    h.options.set(Options.Agent.Anthropic.PromptCacheTTL, "1h")   # live ref -> no rebuild
    assert h.runtime.new("root").acquire().agent is agent
    assert calls["n"] == 1
    assert captured["ttl"].get() == "1h"                          # but the held ref reads the new value


# --------------------------------------------------------------------------- #
# Session ownership: get() identity, across rebuilds
# --------------------------------------------------------------------------- #

def test_get_returns_the_same_session_across_rebuilds():
    h = make_runtime()

    def build(*, checkpointer: AgentCheckpointerService, provider: Annotated[str, Options.Agent.Provider]):
        return object(), PromptEngine()

    register(h.runtime, "root", build)
    s = h.runtime.new("root")
    assert h.runtime.get("root", s.thread_id) is s

    before = s.acquire().agent
    h.options.set(Options.Agent.Provider, "openai")     # rebuild
    assert h.runtime.get("root", s.thread_id) is s       # session identity is stable...
    assert s.acquire().agent is not before               # ...even though the agent under it rebuilt


def test_get_unknown_session_raises():
    h = make_runtime()
    register(h.runtime, "root", _trivial_build)
    s = h.runtime.new("root")
    with pytest.raises(KeyError):
        h.runtime.get("root", "no-such-thread")
    with pytest.raises(KeyError):
        h.runtime.get("other", s.thread_id)


# --------------------------------------------------------------------------- #
# Shared checkpointer
# --------------------------------------------------------------------------- #

def test_one_checkpointer_shared_across_keys_and_sessions():
    h = make_runtime()
    seen = []

    def build(*, checkpointer: AgentCheckpointerService):
        seen.append(checkpointer)
        return object(), PromptEngine()

    register(h.runtime, "a", build)
    register(h.runtime, "b", build)
    h.runtime.new("a").acquire()
    h.runtime.new("b").acquire()
    h.runtime.new("a").acquire()         # 'a' is cached -> no second build

    assert len(seen) == 2                # one build per key
    assert seen[0] is seen[1]            # same checkpointer instance everywhere
    assert isinstance(seen[0], BaseCheckpointSaver)


# --------------------------------------------------------------------------- #
# Context-schema construction: framework fields + service injection + kwargs
# --------------------------------------------------------------------------- #

def test_new_builds_context_with_injection_and_framework_fields():
    h = make_runtime()
    session_factory = object()                                   # stand-in SessionFactoryService
    h.accessor.register(SessionFactoryService, session_factory)

    def build(*, checkpointer: AgentCheckpointerService):
        return object(), PromptEngine()

    register(h.runtime, "root", build)                           # context_schema=RootAgentContext
    ctx = h.runtime.new("root", local_resources=None, hooks=("x",)).agent_context

    assert isinstance(ctx, RootAgentContext)
    assert ctx.session_factory is session_factory                # service field injected from the scope
    assert ctx.runtime is h.runtime and ctx.pending is not None   # framework fields filled by the runtime
    assert ctx.hooks == ("x",)                                   # caller kwarg flowed through


def test_session_queue_is_the_context_pending():
    h = make_runtime()
    register(h.runtime, "root", _trivial_build)
    s = h.runtime.new("root")
    assert s._queue is s.agent_context.pending                   # single source — no second copy
