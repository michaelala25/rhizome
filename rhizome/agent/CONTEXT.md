# rhizome/agent/

> **FOR AGENTS** — Edit this file only when the maintainer explicitly asks you to. It is a
> hand-maintained orientation document, not something to regenerate or "freshen up" on your own
> initiative. When you *are* asked to change it, keep to its style: high-level and human-readable
> (full sentences, breathing room, a guided top-to-bottom flow), describing what the pieces are and
> how they fit together rather than cataloguing methods — the code is the source of truth for detail.
> Resist documenting anything still in flux, and if a section starts drifting toward an API reference,
> trim it back.

This is the agent stack: everything needed to build a language-model agent, hold a conversation with
it, persist that conversation, and branch it into a tree. It is built around a clean separation between
*how an agent is built* (the factory), *who owns the live conversations* (the runtime), and *how those
conversations are arranged* (the graph).

Everything here is provider-neutral and framework-thin: LangGraph supplies the compiled agent and the
checkpointer, and we own the orchestration on top.


## The pieces, and how they fit together

Six objects carry the architecture. Read them in this order — each builds on the last.

- **`AgentFactory`** is a registry. For every *kind* of agent (the root conversation agent, each
  subagent) it holds a declaration: a builder function plus the context/state schemas that agent uses.
  It is pure data — it knows how to describe an agent, not how to build one.

- **`AgentRuntime`** is the workspace's engine. It does two jobs. First, it *builds and caches* the
  compiled agent for each key, lazily and on demand, dropping the cache entry when a relevant option
  changes (so a provider or model switch quietly rebuilds). Second, it *owns every live conversation* —
  it mints sessions and hands them back out by `(key, thread_id)`.

- **`AgentCheckpointer`** is the one shared store that every conversation thread persists into. Thread
  ids keep conversations isolated within it. It is deliberately singular and long-lived: because it
  outlives agent rebuilds, a thread keeps its meaning even after the agent underneath it is swapped.

- **`AgentSession`** is a handle to one conversation — one agent, one thread. You talk to it, and it
  resolves the current compiled agent from the runtime fresh on every turn, so rebuilds are invisible
  to a conversation in progress.

- **`AgentNode`** is a session living as a node in a graph, plus the worker that drives an in-flight
  streaming run. It forwards the session's `send` / `stream` / `invoke` surface.

- **`AgentGraph`** is a branch-and-merge tree of those nodes — the topology that lets one conversation
  fork into several and (experimentally) rejoin.

The ownership, sketched:

```
AgentRuntime ── owns ──┬── the build cache:   key ─────────► (compiled agent, PromptEngine)
                       └── the live sessions: (key, thread) ─► AgentSession

AgentFactory ──────── the declarations the runtime builds from
AgentCheckpointer ─── the single shared store every thread persists into

AgentGraph ── owns ──► AgentNode ── wraps ──► AgentSession
  (a tree of conversations)        (one conversation apiece)
```

The runtime is the center of gravity: the factory feeds it declarations, the checkpointer gives it
persistence, sessions are the conversations it owns, and the graph is an optional layer that arranges
sessions into a branchable structure. You can use sessions directly without ever touching the graph.


## What lives where

- **`factory.py`** — `AgentFactory`, `AgentDeclaration`, and the `AgentFactoryService` the runtime
  depends on. Also the hygiene warnings that flag a builder sidestepping the rebuild contract.
- **`runtime.py`** — `AgentRuntime` and its `AgentRuntimeService` alias. The build cache, invalidation
  wiring, and session ownership.
- **`checkpointer.py`** — the shared `AgentCheckpointerService` (an `InMemorySaver` today) and its
  builder.
- **`session.py`** — `AgentSession`, plus `AgentInstance` (the per-turn bundle of agent + engine +
  context + config) and `InvokeResult`.
- **`graph.py`** — `AgentGraph` and `AgentNode`, the branch/merge topology, and the `Cursor` alias.
- **`context.py`** — the per-conversation context schemas (`BaseAgentContext` and `RootAgentContext`):
  the live channels and services an agent runs against.
- **`streaming.py`** — `AgentStreamingContext` (the callback surface a streaming run drives) and
  `RunStateView` (the run-scoped view of state those callbacks see).
- **`base/`** — the leaf vocabulary the rest of the stack shares: the payload input language
  (`MessagePayload`, `StateUpdatePayload`, `PayloadQueue`), the cleanup-request types, and the
  `ConsumedResources` snapshot. It imports nothing else under `agent`, so `engine`, `state`, and
  `tools` can all depend on it without cycles.
- **`engine/`** — the prompt engine: turns conversation state into the actual model request. The payload
  input language a session feeds it lives in `base` (and is re-exported from here). See _The prompt
  engine_, below, and `engine/CONTEXT.md`.
- **`state.py`** — the graph state schema (`RootAgentState`: messages, mode, verbosity, loaded resources,
  workflow proposal state).
- **`prompts/`** and **`tools/`** — the system prompt / guides / tool allowlists, and the agent's tools.


## Declaring an agent kind

An agent kind is registered on the factory with a *builder*. The builder's parameters are its
dependencies, supplied by annotation: services are injected (see `rhizome/utils/services.py`), and
options are *bound* — and the form of the annotation decides what happens when that option later changes.

- `Annotated[T, spec]` binds the option's **current value**, baked into the agent at build time. The
  runtime treats such an option as structural: **when it changes, the cached agent is discarded and
  rebuilt.** Use it for anything that changes the agent's shape — provider, model, tool set.
- `Annotated[OptionRef[T], spec]` binds a **live handle** instead of a value. Nothing is baked in —
  whoever holds the ref (typically the prompt engine) reads the current value fresh each turn, so
  **changing the option does _not_ rebuild the agent.** Use it for behavioral knobs honored on the fly,
  like a cache TTL or a verbosity dial.

The builder returns the compiled agent paired with its prompt engine (explained just below).

```python
def build_my_agent(
    *,
    checkpointer: AgentCheckpointerService,                        # injected service
    provider: Annotated[str, Options.Agent.Provider],             # snapshot value → changing it REBUILDS
    cache_ttl: Annotated[OptionRef[int], Options.Agent.CacheTtl],  # live handle → no rebuild on change
):
    engine = MyPromptEngine(cache_ttl)             # the engine reads the live ref on each turn
    agent = create_agent(
        model=make_model(provider),
        tools=[...],
        middleware=[PromptCompilerMiddleware(engine)],
        context_schema=MyContext,
        checkpointer=checkpointer,
    )
    return agent, engine

factory.register("my_agent", build=build_my_agent, context_schema=MyContext)
```


## The prompt engine

Every builder returns a *pair* — the compiled agent and a **prompt engine** — and the engine is worth a
word, because needing one at all is not obvious.

The engine owns the question of *what the model actually sees on each turn*. It takes the conversation's
state (messages, mode, loaded resources, ...) and assembles the real request: the system prompt, where
resource context sits, how ephemeral reminders are ordered, where prompt-cache breakpoints fall. It is
the one place that knows how to turn durable conversation state into a concrete wire prompt.

It is built per agent kind and is option-derived, exactly like the agent — the cache-breakpoint policy,
for instance, is provider-specific (an Anthropic engine places breakpoints; an OpenAI one cannot). So it
shares the agent's build-and-invalidate lifecycle: change a structural option and the agent and its
engine rebuild together.

That is why each declaration constructs an engine and hands it back, rather than the runtime supplying a
shared one. Two consumers need *the same* engine instance: the agent's `PromptCompilerMiddleware` calls
it *during* a run to compile the prompt, and the session calls it *between* runs to repair a conversation
that a broken run left mid-tool-call. Because the builder is the only thing that ever creates the engine,
those two are guaranteed to be the identical object and cannot drift apart.


## Context vs. state

Two schemas attach to an agent, and they are easy to mix up.

The **context schema** (a `BaseAgentContext` subclass such as `RootAgentContext`) is the per-conversation
bag of *services and channels* the agent runs against: the live payload queue, the resource stores, app
hooks, the database session factory. The runtime instantiates one per session in `new()`. It is immutable
for the duration of a run (LangGraph's rule), but the objects behind it are live — and it is **not**
checkpointed, so it is re-supplied on every run and rides through rebuilds untouched.

The **state schema** (a `BaseAgentState` subclass such as `RootAgentState`, a `TypedDict`) is the *graph
state* that flows through the agent and
**is** checkpointed: the message history, the current mode and verbosity, which resources are loaded, any
in-progress workflow proposals. This is what `session.agent_state` reads and what `branch` copies into a
child thread.

The short version: **state is what a branch must inherit; context is the wiring it runs against.**


## Creating and running a session

You do not construct sessions yourself — the runtime owns them. (You also rarely construct the runtime
yourself; it is injected from the service container.)

```python
session = runtime.new("my_agent")     # fresh thread; any context-schema fields are passed as kwargs
session.send(MessagePayload(data="hello", role=MessagePayload.Role.USER))
result = await session.invoke()       # one-shot; returns an InvokeResult
```

Input arrives as **payloads**, not raw messages — `send()` queues them and the prompt engine ingests
them at the next model call. A payload sent while the session is idle waits in a backlog for the next
run; a payload sent mid-run with `send(..., eager=True)` is picked up by the run already in flight.

There are two ways to run a turn:

- **`invoke()`** is the one-shot shape: run to completion, get an `InvokeResult` back (the final
  message, the full state, any structured output, the thread id). Natural for subagent calls.
- **`stream()`** is the interactive shape: hand it an `AgentStreamingContext` and the run feeds events,
  interrupts, and lifecycle moments back through its callbacks. Nothing reaches around the session to
  inspect the thread mid-run — the callbacks are the whole story, including a `RunStateView` of state
  as of each event.

### Stateful vs. stateless

The distinction is just *whether you keep the thread*.

- **Stateful** — hold onto the session (or just its `thread_id`) and keep using it; the shared
  checkpointer carries the history forward across turns. To resume later, or from elsewhere, call
  `runtime.get(key, thread_id)`. This is how a tool talks to a subagent across several turns: it emits
  the subagent's `thread_id` in a tool message and re-fetches that same conversation next time.
- **Stateless** — call `runtime.new(...)` for a fresh thread, run one turn, and let it go. Each call
  starts clean.

The root agent and subagents are the same machinery — both are just sessions the runtime owns, reached
through the same interface.


## The conversation graph

`AgentGraph` arranges sessions into a tree so a single conversation can fork. Each `AgentNode` is one
conversation; the graph is the only thing that creates, freezes, branches, and merges them.

The topology opens lazily: construct the graph, then call `make_root()` once to mint the root node.
From there:

- **`branch(at)`** forks a new child off a node's current state. The child's thread is seeded from the
  parent by a checkpoint copy that preserves message ids, so the child continues the parent's prompt
  prefix exactly (this is what keeps the model's prefix cache warm). The parent then *freezes*.
- **`merge(into, from_)`** (experimental) creates a fresh child below two parents, unioning their
  histories onto the `into` spine.

The rule that ties it together: **only leaves talk.** A node freezes the moment it gains children, so
frozen nodes are history and the live conversation always lives at the leaves. Locations in the tree
are addressed by a **`Cursor`** (a root-to-node path), which matters because a merged node is reachable
by more than one lineage.

`AgentGraph`/`AgentNode` are deliberately abstract — they know about topology and checkpoint seeding
and nothing else. Per-node extras (a display feed, a branch name, node-local resource stores) belong to
subclasses, which carry them across a branch/merge edge through the `AgentNode.derive` hook. The app's
`ConversationGraph` (in `rhizome/app/chat_area/`) is that subclass.


## Seams to the rest of the app

- **Resources** (`rhizome/resources/`) reach an agent as stores on its context, which the prompt
  engine reads at compile time. The abstract layer here is unaware of them; the conversation layer wires
  them in.
- **The app** (`rhizome/app/chat_area/`) drives all of this: `ConversationGraph` extends `AgentGraph`,
  and `ChatAreaModel` is the view-model that owns a graph and turns user actions into payloads and runs.
