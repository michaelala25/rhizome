# rhizome/app/commit_proposal/

VM side of the commit-proposal review surface. The agent drafts a batch of `Entry` dataclasses
and hands them off; the user reviews/edits/excludes/cancels via the TUI in
`rhizome/tui/widgets/commit_proposal/`.

## Files

- **entry.py** — `Entry` dataclass (title, content, `EntryType`, topic_id/name) plus the
  `cycle_entry_type` helper used by the `f` keybinding. Mutable; `.clone()` snapshots a row so
  the parent VM's `reset()` can restore the original proposal after in-place edits.

- **commit_proposal.py** — `CommitProposalModel`. Owns the entry list, cursor, exclusion set,
  edit-instructions buffer, and the `EDITING → DONE` lifecycle. Holds one child
  `EntryDetailsModel` that's reseeded on every cursor move. See the file docstring for the full
  state contract.

- **entry_details.py** — `EntryDetailsModel`. Per-entry buffered edit of title/content, mirroring
  the browser's entry-details VM in shape. `accept()` writes back to the in-memory `Entry`
  in place; there is no DB write here — the parent VM commits the whole proposal as a unit.

## Notes

- The entry list size is fixed at construction. `reset()` re-clones rather than re-creating the
  list, so the view's one-time-`add_row` pattern stays valid.
- Cursor-move-while-dirty silently discards the unsubmitted edit, matching the browser policy.
