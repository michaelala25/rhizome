# rhizome/db/

Database layer. Defines the ORM schema and provides async engine/session management.

## Files

- **models.py** — SQLAlchemy ORM models (all use `Mapped`/`mapped_column` typed syntax). Also defines `UTCDateTime`, a `TypeDecorator` wrapping `DateTime(timezone=True)` that rejects naive datetimes on write and re-attaches `tzinfo=UTC` on read — required because SQLite silently drops tzinfo, and `py-fsrs` expects aware UTC datetimes.
  - `Topic` — knowledge area organized as a tree (adjacency list via `parent_id` self-FK). Root topics have `parent_id=NULL`. Sibling names must be unique (`UniqueConstraint("parent_id", "name")`). Owns entries via cascade delete.
  - `KnowledgeEntry` — core knowledge unit (fact/concept/definition). Has `title`, `content`, `additional_notes`, `entry_type`, `difficulty` (nullable int), `speed_testable` (bool, default false). Connected to tags (many-to-many) and other entries (directed graph).
  - `Tag` — freeform label (unique, lowercase-normalized).
  - `KnowledgeEntryTag` — junction table for entry-tag many-to-many.
  - `RelatedKnowledgeEntries` — directed edge between two entries with a `relationship_type` (e.g. "depends_on", "example_of"). Has a CHECK constraint preventing self-loops; cycles are prevented at the tool layer.
  - `ReviewSession` — a review session covering a set of topics and entries. Has `ephemeral` (bool, default false) to flag sessions that should be cleaned up periodically. Tracks `created_at` (auto), `started_at` (auto), and optional `completed_at`. Has optional `additional_args` (JSON) for flexible metadata, optional `user_instructions` (text) for user-provided session guidance, optional `plan` (text) for the agent's discussion plan, and optional `final_summary` (text) for post-session thoughts. Has topics (M2M via `ReviewSessionTopic`), entries (M2M via `ReviewSessionEntry`), interactions (one-to-many cascade), and flashcards (one-to-many, SET NULL on delete — flashcards survive session deletion with `session_id` nullified).
  - `ReviewSessionTopic` — junction table: review session ↔ topic. Composite PK on (`session_id`, `topic_id`).
  - `ReviewSessionEntry` — junction table: review session ↔ entry. Composite PK on (`session_id`, `entry_id`).
  - `Flashcard` — reusable question template tied to a topic (`topic_id` FK, indexed) and optionally to the review session that created it (`session_id` FK, nullable, indexed). Has `question_text`, `answer_text`, and optional `testing_notes` (instructions for critiquing user responses). Associated to knowledge entries via `FlashcardEntry` junction (M2M, cascade delete-orphan). When a parent ReviewSession is deleted, the flashcard's `session_id` is set to NULL (the flashcard is preserved). Also carries FSRS scheduling state: `fsrs_state` (int, default 1; matches `py-fsrs` `State` IntEnum — 1=Learning, 2=Review, 3=Relearning), `fsrs_step` (nullable int, default 0), `stability` / `difficulty` (nullable floats), `due` (nullable `UTCDateTime`, indexed), `last_review` (nullable `UTCDateTime`). `due IS NULL` means the card is parked (not scheduled); `get_due_flashcards` queries filter `WHERE due IS NOT NULL AND due <= now()`. The partial index on `due` skips NULLs (SQLite default) so parked cards don't bloat it.
  - `FlashcardEntry` — junction table: flashcard ↔ knowledge entry. Composite PK on (`flashcard_id`, `entry_id`).
  - `ReviewInteraction` — one review checkpoint within a session. Has optional `flashcard_id` FK (indexed) — present for flashcard-based reviews, null for conversational exchanges. Has optional `summary` (brief note on what was covered/assessed), `score` (1-4, CHECK constraint — aligned with FSRS Rating values), and `position` for ordering. References entries tested via `ReviewInteractionEntry` junction.
  - `ReviewInteractionEntry` — junction table: review interaction ↔ entry. Composite PK on (`interaction_id`, `entry_id`).

- **engine.py** — Engine, session, and initialization:
  - `get_engine(db_path)` — creates an `AsyncEngine` using `sqlite+aiosqlite`. Registers a `connect` event listener that enables SQLite foreign key enforcement (`PRAGMA foreign_keys = ON`) on every new DBAPI connection.
  - `get_session_factory(engine)` — returns an `async_sessionmaker` with `expire_on_commit=False`.
  - `run_migrations(db_path)` — runs all pending Alembic migrations against the given DB. No-op if already at the latest revision.
  - `init_db(db_path)` — synchronous entry point for app startup. Calls `run_migrations()` then returns an engine with FK enforcement ON.

- **alembic/** — Alembic migration environment:
  - `env.py` — configured for async SQLAlchemy with `render_as_batch=True` (required for SQLite `ALTER TABLE` limitations). `target_metadata` points to `Base.metadata` for autogenerate support.
  - `versions/` — migration scripts. Each has `upgrade()` / `downgrade()` functions and a revision chain (`revision` / `down_revision`). Generate new migrations with `uv run alembic revision --autogenerate -m "description"`.
  - `alembic.ini` (repo root) — Alembic config. `sqlalchemy.url` is a fallback for CLI use; `init_db()` overrides it programmatically.

## FK Cascade Behavior

All foreign keys define explicit `ON DELETE` behavior at the database level, enforced by SQLite's `PRAGMA foreign_keys = ON`:
- Most FKs use `ON DELETE CASCADE` — deleting a parent row automatically deletes dependent rows (e.g., deleting a topic cascades to its entries, flashcards, junction rows, etc.).
- `review_interaction.flashcard_id` uses `ON DELETE SET NULL` — deleting a flashcard nullifies the reference rather than deleting the interaction.
- `flashcard.session_id` uses `ON DELETE SET NULL` — deleting a review session preserves its flashcards (nullifying the back-reference).

This matches the ORM-level cascade settings on relationships, but also applies to raw SQL operations.

## `__init__.py` exports

All 13 model classes (`Base`, `Topic`, `KnowledgeEntry`, `Tag`, `KnowledgeEntryTag`, `RelatedKnowledgeEntries`, `Flashcard`, `FlashcardEntry`, `ReviewSession`, `ReviewSessionTopic`, `ReviewSessionEntry`, `ReviewInteraction`, `ReviewInteractionEntry`), plus `EntryType`, `get_engine`, `get_session_factory`, and `init_db`. Import from `rhizome.db` directly.
