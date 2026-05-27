# rhizome/tui/widgets/browser/knowledge_entry_tab/linked_flashcards/

Right-hand panel of `KnowledgeEntryBrowserTabView` when the parent tab is in
`State.LINKED_FLASHCARDS`. Shows the flashcards linked to the current entry-id
set (single-select cursor entry, or the selection union in multi-select). In
relink mode the panel reshapes into a dual-section editor for picking which
flashcards stay linked.

## Files

- **view_model.py — `LinkedFlashcardsPanelViewModel`** (`QueryBackedViewModel`
  + `SearchableViewModelMixin`): owns both display sections, search, topic
  filter, cursor, and relink selection. Module docstring covers the dual-
  section contract + fetch protocol + accept/cancel diff semantics.

- **view.py — `LinkedFlashcardsPanelView`**: search bar / `DataTable` /
  Accept-Cancel choices / answer preview / status row. Internal widgets:
  - `_LinkedFlashcardsTable` — `DataTable` subclass with auto-load-more
    at the bottom edge and a `space` binding for the relink-set toggle.
  - `_FlashcardAnswerPreview` — read-only `TextArea` showing the cursor
    flashcard's question + answer + testing notes.
  - `_RelinkChoicesList` — `ChoiceList` subclass for Accept/Cancel of a
    pending relink edit; visibility driven by `vm.is_relink_dirty` via the
    `.-visible` class toggle managed in `_refresh`.

## Conventions

- Cursor index spans `[*linked, <boundary>, *remaining]` in relink mode; just
  `linked` otherwise. The boundary row exists in relink even when both
  sections are empty so the partition is always visible. `cursor_section()` /
  `cursor_flashcard` resolve where the cursor sits — toggle/preview gate on
  the section being `"linked"` / `"remaining"`, not `"boundary"`.

- The pinned (currently-linked) section is frozen at fetch time. Toggling a
  pinned row off marks it for unlink but does **not** reposition the row —
  the partition stays stable so accept can diff against the baseline.

- Relink baseline (`_relink_baseline_ids`) is recomputed from the live
  pinned section, so it reseeds automatically on every successful fetch.
  Pool rows always start unselected.

- `_RelinkChoicesList` is *not* refreshed via `prepare_for_show` — the
  widget is mounted once and toggled by CSS class, so its cursor persists
  across show/hide cycles (intentional: pressing Cancel and then editing
  again lands you back where you were rather than snapping to Accept).
  It owns its own `←/→/enter/esc` bindings via the base `ChoiceList`.

- `relink_choices` *is* a participant in the parent tab's `alt+arrow`
  focus graph (gated on `vm.linked_flashcards.is_relink_dirty`). See the
  parent `CONTEXT.md` "Cross-region focus" table.

- Three-state colouring on flashcard rows mirrors the entries-side table:
  non-relink zebra; relink-unselected darker zebra (`-relink` class flips
  the palette); relink-selected bright green (`#5fd75f`, bold).
