# rhizome/tui/widgets/flashcard_review/

The `FlashcardReview` widget — an interactive Textual UI for working through a batch of flashcards in one sitting. Cards advance through the FSRS scheduler in memory; nothing is persisted to the DB until the widget's caller invokes `view_model.commit()`.

## Architecture

The widget is split into a view-model and a view, communicating through a single observer list:

- **`FlashcardReviewViewModel`** owns *all* mutable state for the session: card list, cursor index, round-progress queues, the auto-score task handle, mode flags (auto-score enabled, auto-approve, timers visible, help visible, collapsed), the latest-action message slot, and the per-card `Flashcard` instances. The view-model is a state machine with three top-level states (`START`, `REVIEWING`, `DONE`) and a per-card state machine inside each `Flashcard` (`FRONT`, `REVEALED_NOT_SCORED`, `REVEALED_PENDING_AUTO_SCORE`, `SCORED_PENDING_APPROVAL`, `SCORED`, `AWAITING_REVEAL`). Subclasses `ViewModelBase` (see `widgets/view_model_base.py`), inheriting the `dirty` and `focus` `CallbackGroup`s plus the `emit` / `emit_once` / `subscribe` API; fires `dirty` exactly once per public transition method, with batching via `emit_once` where a single user action triggers multiple internal state changes (`score_current_card`, `reset_current_card`, `toggle_skip_current_card`, `accept_all_auto_scores`, and the async tail of `_handle_batched_auto_score`).

- **`FlashcardReview`** (the view) is a Textual widget that subscribes a single `_refresh` listener to `vm.dirty` and re-reads the entire VM on every emit. It holds *no* session state of its own — only widget-local concerns: interval handles for the live timer / due countdown / throbber, the throbber frame counter, and the message-flash timer. All keyboard input is forwarded to `vm.on_key(event)`; the view never mutates VM state directly. It's a dumb mirror.

This split means a transition flow looks like:
1. User presses a key → Textual delivers `events.Key` to the view
2. View calls `vm.on_key(event)`
3. VM dispatches by current state, mutates internal state, emits `dirty`
4. View's `_refresh` listener runs, reads VM state, repaints the widgets

## Files

- **`view_model.py`** — `FlashcardReviewViewModel` and `Action` enum. The module docstring contains the full state-machine documentation: top-level state transitions, the round-queue invariants (`_remaining_before_batched_autoscore` / `_next_remaining_before_batched_autoscore`), the `_check_ready_to_autoscore` / `_check_done` invariants and the sites that must call them, the auto-scoring batch lifecycle, and cursor management semantics. Read this first when changing scoring or round-progression logic.

- **`flashcard.py`** — `Flashcard` and `FlashcardData`. The module docstring contains the per-card state machine: every transition, the `Score` enum, the FSRS state ownership rules (`_initial_fsrs_card` / `_current_fsrs_card`), `pause`/`unpause`/`reset` contracts, and the auto-scoring failure / discard latches. Read this first when changing card-level transitions or rating semantics.

- **`view.py`** — `FlashcardReview` (the Textual widget) and `_AnswerInput`. Composes the layout, runs `_refresh_*` methods that read the VM and repaint each region, owns the live-timer / throbber / due-countdown intervals reconciled against VM state, and pops `vm.latest_message` on each refresh to flash a 3-second action message in the bottom-right slot. View-only concerns; should not contain session logic.

- **`_dot_strips.py`** — `_DotStrip(Static)` — the per-card dot row at the bottom of the widget. One glyph per card encoding lifecycle (unscored / awaiting-review / scored / skipped), color encoding attention (pending auto-score / pending approval / failed). Cursor swaps the glyph; flagged overlays an underline. Scrolls horizontally to keep the cursor visible when there are more cards than fit on-screen.

- **`_timer.py`** — `Timer` — start / pause / stop / reset / elapsed-seconds wall-clock helper. Used for both think-time (per `Flashcard`) and the due-countdown after a requeue.
