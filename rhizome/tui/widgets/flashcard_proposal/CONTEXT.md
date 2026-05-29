# rhizome/tui/widgets/flashcard_proposal/

View side of the flashcard-proposal review surface. Bound to `FlashcardProposalVM` /
`FlashcardDetailsVM` in `rhizome/app/flashcard_proposal/`.

Direct mirror of `rhizome/tui/widgets/commit_proposal/`. The flashcard variant has one extra
editable field (testing notes) and one read-only display field (linked knowledge entries) in the
details panel; otherwise the structure — focus graph, lifecycle bindings, dynamic-width table
column, edit-instructions chord — is the same.

## Files

- **view.py** — `FlashcardProposal` parent view. Composes the five focusable regions
  (shared-topic setter, flashcard list, flashcard details, global choices, edit-instructions
  area) and orchestrates inter-region focus via a static graph + `alt+arrow` bindings. Owns the
  topic-picker modal (needs `session_factory`) and the lifecycle keybindings
  (`ctrl+a/r/c`, `ctrl+e`, `shift+t`). Runs the programmatic `_sync_list_area_height` that pins
  the bordered flashcard-list-area to the taller of itself and the details panel.

- **shared_topic_setter.py** — single-line focusable strip at the top. `enter` opens the topic
  picker (`scope="all"`) via `SetTopicRequested`.

- **flashcard_list.py** — `DataTable` of pending flashcards. Three columns: Question / Topic /
  Answer. Answer is sized dynamically each resize to absorb the remaining horizontal budget.
  Cursor color is overridden per row so excluded flashcards render with a muted-blue cursor; that
  hook bypasses DataTable's `lru_cache` (see the monkey-patch note in the file).

- **flashcard_details.py** — three editable `ConfirmableTextArea`s (Question / Answer / Testing
  Notes) plus a read-only `Static` for the linked knowledge-entry IDs, plus an Accept/Cancel
  `ChoiceList` that appears only when any of the three editable buffers is dirty. The
  linked-entries Static is intentionally outside the focus graph — read-only display.

- **edit_instructions.py** — natural-language buffer at the bottom, visible only while
  `vm.edit_instructions_visible`. Double-`escape` (within 500ms) clears it. `check_action`
  bubbles `up` at row 0 so the parent's focus-graph binding can fire.

- **choices.py** — the always-present Approve / Edit / Reset / Cancel menu
  (`FlashcardProposalChoices`, a `ChoiceList`). Keybinding column on the left, descriptive text
  on the right.

- **messages.py** — `SetTopicRequested(scope)` posted by the leaves; the parent view owns the
  modal-push response.

## Conventions

- VMs live in `rhizome/app/flashcard_proposal/`; this directory contains rendering and key
  routing only.
- Plain `up` / `down` from a leaf bubbles to the parent's `navigate_cursor` binding when at a
  boundary, via either `check_action` (DataTable, edit-instructions) or by not binding the key
  locally (shared-topic-setter, choices).
- Per-region IDs (`fp-shared-topic-setter`, `fp-flashcard-list`, `fp-details-question`, …) are
  the keys of the focus graph in `view.py` — keep them in sync if regions are renamed.
- The linked-entries Static (`fp-details-linked-entries`) is rendered between testing-notes and
  the details choices but is **not** a focus-graph node. If it ever becomes editable, add the id
  to the graph and wire a sibling buffer onto `FlashcardDetailsVM`.
