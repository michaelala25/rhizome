# rhizome/app/flashcard_proposal/

VM side of the flashcard-proposal review surface. The agent drafts a batch of `Flashcard`
dataclasses and hands them off; the user reviews/edits/excludes/cancels via the TUI in
`rhizome/tui/widgets/flashcard_proposal/`.

Direct sibling of `rhizome/app/commit_proposal/` — the two surfaces share the same MVVM shape
(parent VM owning a list + cursor + exclusion set + edit-instructions buffer + lifecycle, plus a
child details VM for buffered per-item edits). Differences from commit-proposal are scoped:

- the per-item dataclass is `Flashcard` (question / answer / testing_notes / entry_ids / topic),
- there is no entry-type cycling,
- the details VM has three editable fields (question / answer / testing_notes) and exposes
  `entry_ids` as a read-only mirror (the view renders linked entries but cannot mutate them).

## Files

- **flashcard.py** — `Flashcard` dataclass with `clone()` + `from_dict()`. Mirrors
  `commit_proposal.entry.Entry`; carries denormalized `topic_id` + `topic_name`.

- **flashcard_proposal.py** — `FlashcardProposalModel`. Owns the flashcard list, cursor, exclusion
  set, edit-instructions buffer, and `EDITING → DONE` lifecycle. Holds one child
  `FlashcardDetailsModel` that's reseeded on every cursor move. See the file docstring for the full
  state contract.

- **flashcard_details.py** — `FlashcardDetailsModel`. Per-flashcard buffered edit of three string
  fields, mirroring the commit-proposal details VM in shape. `accept()` writes back to the
  in-memory `Flashcard` in place; there is no DB write here — the parent VM commits the whole
  proposal as a unit.

## Notes

- The flashcard list size is fixed at construction. `reset()` re-clones rather than re-creating
  the list, so the view's one-time-`add_row` pattern stays valid.
- Cursor-move-while-dirty silently discards the unsubmitted edit, matching the browser policy.
- `entry_ids` is treated as immutable from the widget's perspective. If/when an "edit linked
  entries" affordance lands, it would extend `FlashcardDetailsModel` with a fourth buffer rather
  than mutating the dataclass directly.
