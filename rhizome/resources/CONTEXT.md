# rhizome/resources/

Resource processing services — higher-level operations on document resources that go beyond simple CRUD.

## Modules

- **manager.py** — `ResourceManager`: tracks load state in minimum-description-length (MDL) form — a flat `dict[NodeKey, LoadMode]` where `NodeKey = tuple[Literal["resource","section"], int]`. An entry at a node means "this node and every descendant are loaded at this mode" unless a descendant overrides with its own entry. The `ResourceLoader` widget pushes full snapshots via `set_state()` on every user toggle; `AgentSession.stream()` calls `consume()` at the start of each stream to get the key-level diff against the last consumed snapshot. Also hosts the embedding lifecycle (`ensure_embedded()`, `is_embedding_in_progress()`) used by the loader to compute vector-store embeddings on demand. Exports `LoadMode`, `NodeKey`, `NodeKind`, `ResourceManager`, `ResourceStateChange`.
- **context_message.py** — Builds the `HumanMessage` blocks injected into the agent graph for context-stuffed content. `build_resource_context_message(resource, cs_entries)` takes a resource (with `content` and `sections` eagerly loaded — see `db.operations.get_resource_with_content_and_sections`) plus the subset of MDL entries that are CS'd, and returns a `HumanMessage` with deterministic id `rhizome-resource-ctx-{resource_id}` for in-place replacement via `add_messages`. One text block per CS entry: a `("resource", rid)` entry emits the full `raw_text`, a `("section", sid)` entry emits `raw_text[start_offset : same-or-shallower-next)`. No per-DB-section splitting and no deduplication needed — the MDL invariant guarantees CS entries never overlap.

## Subpackages

- **`extraction/`** — Automatic section/subsection discovery pipeline. Combines format-specific heuristic extraction with LLM-based refinement to produce hierarchical section trees from documents.

## Relationship to Other Modules

- **`rhizome/db/`** — This package does NOT handle persistence. Database models (`Resource`, `ResourceSection`, `ResourceChunk`) and operations live in `rhizome/db/`.
- **`rhizome/agent/tools/`** — Agent tools call into this package to trigger section detection. This package has no dependency on the agent layer.
- **`rhizome/tui/widgets/`** — `ResourceLoader` holds the authoritative MDL state and pushes snapshots to `ResourceManager.set_state()` on every user toggle. This package has no dependency on the TUI layer.
- **`rhizome/agent/session.py`** — `AgentSession` holds a `ResourceManager` reference and calls `consume()` at the start of each `stream()` invocation.
