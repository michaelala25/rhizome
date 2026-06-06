# rhizome/tui/widgets/commit_proposal/

View side of the commit-proposal review surface. Bound to `CommitProposalModel` /
`EntryDetailsModel` in `rhizome/app/commit_proposal/`.

## Files

- **view.py** — `CommitProposal` parent view. Composes the five focusable regions
  (shared-topic setter, entry list, entry details, global choices, edit-instructions area) and
  orchestrates inter-region focus via a static graph + `alt+arrow` bindings. Owns the
  topic-picker modal (needs `session_factory`) and the lifecycle keybindings
  (`ctrl+a/r/c`, `ctrl+e`, `shift+t`). Also runs the programmatic
  `_sync_entry_area_height` that pins the bordered entry-list-area to the taller of itself
  and the details panel — keeps the global choices row from drifting away from the table.

- **shared_topic_setter.py** — single-line focusable strip at the top. `enter` opens the
  topic picker (`scope="all"`) via `SetTopicRequested`.

- **entry_list.py** — `DataTable` of pending entries. Four columns: Title / Type / Topic /
  Content. Content is sized dynamically each resize to absorb the remaining horizontal
  budget. Cursor color is overridden per row so excluded entries render with a muted-blue
  cursor; that hook bypasses DataTable's `lru_cache` (see the monkey-patch note in the file).

- **entry_details.py** — title + content `TextArea`s plus an Accept/Cancel `ChoiceList` that
  appears only when the per-entry edit is dirty.

- **edit_instructions.py** — natural-language buffer at the bottom, visible only while
  `vm.edit_instructions_visible`. Double-`escape` (within 500ms) clears it. `check_action`
  bubbles `up` at row 0 so the parent's focus-graph binding can fire.

- **choices.py** — the always-present Approve / Edit / Reset / Cancel menu (`CommitProposalChoices`,
  a `ChoiceList`). Keybinding column on the left, descriptive text on the right.

- **messages.py** — `SetTopicRequested(scope)` posted by the leaves; the parent view owns the
  modal-push response.

## Conventions

- VMs live in `rhizome/app/commit_proposal/`; this directory contains rendering and key
  routing only.
- Plain `up` / `down` from a leaf bubbles to the parent's `navigate_cursor` binding when at
  a boundary, via either `check_action` (DataTable, edit-instructions) or by not binding
  the key locally (shared-topic-setter, choices).
- Per-region IDs (`cp-shared-topic-setter`, `cp-entry-list`, `cp-details-title`, …) are the
  keys of the focus graph in `view.py` — keep them in sync if regions are renamed.
