# rhizome/resources/

The resource loading layer: how documents become the text — and the search index — an agent sees. The
package splits in two: the **load-state stores**, which are the de facto API, and a set of **carry-over
ingestion utilities** kept around but not yet wired into the live flow.

## The stores (de facto API)

A content-free skeleton, the load-state arithmetic over it, the per-channel stores, and the content
builders that turn loaded nodes into model-visible text. See each module's docstring for detail.

- **`tree.py`** — `ResourceTree` / `ResourceTreeNode`: the resource/section hierarchy as a content-free skeleton (ids + structure only, never content). Built eagerly, owned once per workspace, shared by every store; `refresh()` re-pulls it after resource/section CRUD.
- **`store.py`** — load-state arithmetic plus the store objects (`ResourceContextStore`, `ResourceIndexStore`). A *description* is a set of tree nodes meaning "these subtrees are loaded", kept in canonical minimal form so set equality is a sound "nothing changed" check and set difference a well-defined delta. The algorithms are free functions over `(sets, tree)`; the stores are dumb containers. Cross-store policy (global/local disjointness) and consumption bookkeeping deliberately live with the writer/consumer, not here.
- **`content.py`** — `build_resource_block` (context channels: a loaded subtree rendered as context-stuffed text) and `build_index_block` (index channel: a metadata-only listing of everything searchable). Free functions over the DB session; the prompt engine wraps their output in well-known-id messages.
- **`index.py`** — `ResourceVectorStore`: a flat FAISS index over the *precomputed* embeddings of the currently-loaded chunks. It never calls an embedding model — producing those bytes happens at ingest time.

## Carry-over ingestion utilities (pending re-wiring)

These have no live consumer right now — their previous driver was retired — and are kept pending re-integration into the new resource-creation flow. Each carries a TODO at the top of its file.

- **`ingest.py`** — text extraction, token estimation, and resource creation.
- **`embeddings.py`** — chunking plus the Voyage embedding round-trip that produces the bytes `index.py` reads.
- **`auto_metadata.py`** — LLM title/summary generation from a resource's opening tokens.
- **`extraction/`** — automatic section/subsection discovery (format heuristics + LLM refinement).

## Relationship to Other Modules

- **`rhizome/db/`** — This package does NOT handle persistence. Resource models (`Resource`, `ResourceSection`, `ResourceChunk`) and operations live in `rhizome/db/`.
- **`rhizome/agent/`** — the prompt engine holds the stores on its context and calls into `content.py` / `index.py` each turn to assemble what the model sees; see `rhizome/agent/engine/`.
- **`rhizome/app/resource_loader/`** — the panel VM that writes load-state into the stores (and owns the cross-store global/local policy) on every user toggle.
