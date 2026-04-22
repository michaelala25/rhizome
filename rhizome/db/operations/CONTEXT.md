# rhizome/db/operations/

Async tool functions for the agent/TUI to interact with the database. Every public function takes an `AsyncSession` as its first argument and uses keyword-only args for parameters. Functions call `session.flush()` but never `session.commit()` — the caller controls transaction boundaries.

## Files

- **topics.py** — `create_topic` (with optional `parent_id`), `get_topic`, `list_root_topics`, `list_children`, `get_subtree` (recursive CTE, depth-limited to 10), `update_topic`, `delete_topic`. Topics form a tree via adjacency list (`parent_id` self-FK). Deleting a topic cascades to its entries but not to child topics (FK constraint prevents deleting a topic with children).

- **entries.py** — `create_entry`, `get_entry`, `list_entries`, `update_entry`, `delete_entry`, `search_entries`. Entries have an `entry_type` (default "fact"), `difficulty` (nullable int), and `speed_testable` (bool, default false). `search_entries` does case-insensitive LIKE on title+content, with optional `topic_id` scoping.

- **tags.py** — `create_tag`, `list_tags`, `tag_entry`, `untag_entry`, `get_entries_by_tag`. Tags are lowercase-normalized. `tag_entry` auto-creates the tag if missing and is idempotent. `untag_entry` is a no-op if the tag/association doesn't exist.

- **relations.py** — `add_relation`, `remove_relation`, `get_related_entries`, `get_dependency_chain`, plus `CycleError`. Manages directed edges in the knowledge graph. `add_relation` runs a recursive CTE to detect cycles before inserting. `get_dependency_chain` follows only "depends_on" edges transitively (depth-limited to 10).

- **reviews.py** — `create_review_session` (with topic and entry IDs), `get_review_session`, `complete_review_session` (sets `completed_at`), `add_review_interaction` (with entry IDs, optional feedback/score/flashcard_id, position for ordering), `list_review_interactions` (ordered by position), `get_review_session_entries` (returns entry IDs in the session pool), `get_sessions_by_topics` (recent non-ephemeral sessions ranked by topic IoU), `update_session_ephemeral`, `update_session_instructions`, `update_session_summary`, `get_interaction_stats` (aggregate scores and per-entry breakdown).

- **flashcards.py** — `create_flashcard` (with topic_id, question/answer text, entry_ids, optional testing_notes and session_id), `list_flashcards_by_topic` (all flashcards for a topic, eager-loads flashcard_entries and session for ephemeral detection), `count_flashcards_by_topic` (count for a topic), `list_flashcards_by_entries` (flashcards linked to given entry IDs, excludes ephemeral), `get_flashcards_by_ids` (with eager-loaded flashcard_entries), `get_flashcard_entry_ids` (resolve flashcard → entry_ids). FSRS scheduling lives here too: `apply_rating(session, flashcard_id, rating)` loads the card's FSRS state, hands it to a freshly-constructed `fsrs.Scheduler` (stateless, cheap — will be replaced with a DB-backed singleton config once the Optimizer flow lands), and writes back the new state (state/step/stability/difficulty/due/last_review); auto-activates parked cards (`due IS NULL → due = now()`). `activate_flashcard` explicitly schedules a parked card without a review. `get_due_flashcards` returns scheduled cards where `due <= now`, oldest-first, excluding parked cards; supports optional `topic_id` filter and `limit`. All datetimes flowing through these functions are aware UTC (enforced by the `UTCDateTime` column type and py-fsrs itself).

- **resolve.py** — `resolve_topic` and `resolve_resource` accept a numeric ID, a plain name (partial case-insensitive match), or a `/`-separated ancestor path for disambiguation (e.g. `"Linux/Filesystem/Types"`). Returns a single model instance on unambiguous match, or a list of `AmbiguousTopic`/`AmbiguousResource` candidates when zero or multiple matches are found. `get_topic_path` builds the full `>` -separated display path for a topic.

## Conventions

- Missing entities raise `ValueError` (for updates/deletes).
- Partial updates: only non-`None` keyword args modify fields.
- All functions are `async def` and return model instances or lists of them.

## `__init__.py` exports

All public functions, `CycleError`, `AmbiguousTopic`, and `AmbiguousResource`. Import from `rhizome.db.operations` directly.
