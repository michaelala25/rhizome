# rhizome/agent/engine/

The prompt engine: the layer that turns a conversation's durable state into the concrete request sent to
the model on each turn — and the payload vocabulary a session feeds in. Every agent kind's builder
constructs an engine, wraps it in a `PromptCompilerMiddleware`, and hands it back to the runtime; the
agent's middleware calls it *during* a run and the session calls it *between* runs (post-mortem repair),
so the two are the same instance by construction.


## The compile / prepare / repair split

`PromptEngine` (in `base.py`) is the whole contract — three methods, divided along one line: whether a
step's output may persist in the checkpoint.

- **`compile`** runs in `before_model` and returns a state update that flows through the reducers and
  lands in the checkpoint — a *fact* about the conversation (ingested payloads, repair patches, injected
  guides, resource context).
- **`prepare`** runs in `wrap_model_call` and reshapes the outgoing request only — a *view* for one wire
  request (message ordering, cache breakpoints), never written back.
- **`repair`** patches orphaned tool calls; pure and idempotent, so it runs both in-stream and post-mortem.

`base.py`'s module docstring carries the message *lifetime* (permanent / semi-permanent reclamation) and
*position* (inline / pinned-to-anchor) vocabulary the layout machinery is built from.


## What lives where

- **`base.py`** — `PromptEngine[C]` (the base contract plus the message-lifetime machinery), the
  `PromptCompilerMiddleware` shim, and the reusable compile primitives (id minting, payload ingestion,
  orphan repair). The base engine is a minimal *working* engine; richer engines override `compile`/`prepare`.
- **`root.py`** — `RootPromptEngine`, the root conversation agent's engine: resource context, the
  vector-index reminder, branch markers, and modes. Holds the branch-marker and mode-guide id schemes.
- **`resources.py`** — the resource-context helpers `RootPromptEngine` drives: `ConsumedResources` (the
  per-thread consumed snapshot), `resource_deltas`, the well-known message-id scheme for resource/index
  blocks, and the tree-grouping / block-building helpers.
- **`cleanup.py`** / **`metadata.py`** — the message-lifetime machinery: marking messages reclaimable and
  reclaiming them on request (`cleanup`), over the per-message tag schema carried in `additional_kwargs`
  (`metadata`).
- **`payload.py`** — the input language (`MessagePayload`, `StateUpdatePayload`, `AgentPayload`) and the
  live `PayloadQueue` a session shares with its engine.


## The one invariant

**Nothing under `engine/` imports from `state`.** State reaches *down* into the engine — `state.py` pulls
`ConsumedResources` and the cleanup request types (`CleanupRequest`, `accumulate_cleanups`) from here —
never the reverse. That one-way rule is what lets `engine/__init__.py` eagerly re-export the public
surface without an import cycle: pulling any engine submodule never re-enters `state` mid-load. It is also
why `ConsumedResources` lives in `resources.py` rather than `state.py`, and why `StateUpdatePayload` is
generic over its schema rather than naming a concrete one. Add a `from ..state import` to any engine
module and the cycle comes back.
