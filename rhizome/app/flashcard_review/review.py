"""View-model for the FlashcardReview widget.

This module owns the session-level state machine ``FlashcardReviewModel`` over a list of
``Flashcard`` instances (per-card state machine documented in ``flashcard.py``). The view
(``FlashcardReview``) subscribes to a single ``dirty`` observer list on the VM and re-renders
on every emit; it never mutates VM state directly.

See ``flashcard.py`` for the Flashcard state machine and FSRS state ownership docs.

============================================================================================
FlashcardReviewModel state machine
============================================================================================

States:
    START      Pre-session. ``current_card`` is None. The user must issue the begin action
               to enter REVIEWING.
    REVIEWING  Active session. The user is rating cards.
    DONE       Terminal. The session is over (either ``cancel()`` or ``finish()`` got us here).
               ``cancelled`` distinguishes the two; ``collapsed`` defaults to True.

Transitions:

    START
        -> REVIEWING [via begin()]
            - The user issued the begin action from the start screen.
            - Unpauses the first card's think-time timer if it is in FRONT.


    REVIEWING
        -> DONE [via finish()]
            - Reached when ``_check_done`` observes the finish-now invariant (every card is in
              state SCORED with score in {HARD, GOOD, EASY, SKIPPED}), or as the tail of
              ``cancel()``.
            - Pauses the current card's think-time timer if it is still running; cancels any
              in-flight autoscore task; converts every AWAITING_REVEAL card to
              SCORED(SKIPPED) (only relevant on the cancel path — the natural-finish path,
              by construction, has no AWAITING_REVEAL cards); sets ``_collapsed = True``.

        -> DONE [via cancel()]
            - The user issued the cancel action.
            - Sets ``_cancelled = True``, then delegates to ``finish()``.


    DONE
        Terminal — no outbound state transitions.

        In-state operations (do not change ``state``):
            - ``toggle_collapsed()`` — flips the ``_collapsed`` view-only flag so the view
              collapses or re-expands the card detail panel. Does not affect the session
              outcome.
            - ``next_card()`` / ``prev_card()`` — cursor navigation through the read-only
              post-session card view.

VM contracts:
    - ``begin()`` asserts state == START. ``cancel()`` and ``finish()`` assert state != DONE.
      ``next_card``/``prev_card``/``score_current_card`` assert state != START. The
      ``score_current_card`` body further asserts state == REVIEWING.
    - ``toggle_collapsed()`` asserts state == DONE.
    - The view subscribes a single ``_refresh`` listener and a single ``_maybe_resolve``
      listener to the ``dirty`` observer list. The VM emits ``dirty`` exactly once per public
      transition method.

============================================================================================
Round queues and the central invariants
============================================================================================

Two sets of card ids drive round progression:

    _remaining_before_batched_autoscore
        Cards still needing attention in the CURRENT round. Drained as the user scores them.
    _next_remaining_before_batched_autoscore
        Cards AGAIN'd during the current round. Will be swapped into _remaining once the
        current round drains AND any pending-AUTO batch has run.

The two load-bearing invariants, each enforced by its own check:

    1. ``_check_ready_to_autoscore``: the auto-scoring batch fires precisely when
       ``_remaining`` drains AND there are cards in REVEALED_PENDING_AUTO_SCORE. On
       drain, ``_next_remaining`` is swapped into ``_remaining`` (opening the next round)
       before the batch is dispatched.
    2. ``_check_done``: the session transitions to DONE precisely when every card is in
       state SCORED with score in {HARD, GOOD, EASY, SKIPPED}. The remaining sets aren't
       consulted — they govern auto-score batch timing only. By construction, any pending
       AUTO card fails this check (state == REVEALED_PENDING_AUTO_SCORE), so we don't need
       a separate ``autoscore_in_progress`` guard.

As a corollary, every site that mutates ``_remaining`` / ``_next_remaining`` or transitions
a card's score state must call BOTH checks afterwards, or the session can stall in REVIEWING
forever. The current sites are:

    - ``score_current_card``   (every score path; calls both at the tail)
    - key event for ``reset``  (in ``_on_key_reviewing``; calls both at the tail)
    - key event for ``skip``   (in ``_on_key_reviewing``; calls both at the tail)
    - key event for ``unskip`` (in ``_on_key_reviewing``; calls both at the tail)
    - ``_handle_batched_auto_score`` (post-batch requeue/failure handling; calls both at
      the tail, which can recursively dispatch a follow-up batch if the user deferred more
      cards while the previous batch was in flight)

Order matters: ``_check_ready_to_autoscore`` runs before ``_check_done``. If a batch was
just dispatched, the cards it operates on aren't terminal yet, so ``_check_done`` correctly
stays its hand.

A second corollary: scoring a card to EITHER state (SCORED or REVEALED_PENDING_AUTO_SCORE)
must remove its id from BOTH queues. ``score_current_card`` discards from both at the top of
the method; the requeue branch (post-rating FSRS state ∈ {Learning, Relearning}) then re-adds
to ``_next_remaining`` at the bottom. Without this, a previously-requeued card that the user
reveals and rates before its due timer fires would leave a ghost id in ``_next_remaining``
that survives the round-swap and blocks finish.

============================================================================================
Auto-scoring batch lifecycle
============================================================================================

    1. ``_check_ready_to_autoscore`` observes ``_remaining`` empty and at least one card in
       state REVEALED_PENDING_AUTO_SCORE. It swaps ``_next_remaining`` into ``_remaining``
       (so the requeued cards from this round — those whose post-rating FSRS state was
       Learning or Relearning — become the next round's queue), then spawns a single
       ``_handle_batched_auto_score`` task.

    2. ``autoscore_in_progress`` (``task is not None and not task.done()``) guards against:
       a. ``_check_ready_to_autoscore`` re-entrance spawning a duplicate task.
        - In other words, we will NEVER risk spawning a duplicate task until the first one
          finishes.
       b. The user manually rating a REVEALED_PENDING_AUTO_SCORE card mid-batch (the
          would-be-overridden card is included in the in-flight batch).
       c. The user resetting a REVEALED_PENDING_AUTO_SCORE card mid-batch.

    3. ``_auto_score`` builds a single prompt covering all pending cards and invokes the
       scorer subagent once. Results are looked up by stable card id. For each card:
       - Score 1/2/3/4 -> ``card.set_score(...)``: scheduler advances FSRS state, then the
                          card lands in SCORED if the new state is Review, or
                          AWAITING_REVEAL if Learning/Relearning. Cards landing in
                          AWAITING_REVEAL are accumulated into ``requeued_cards``.
       - Missing/malformed/out-of-range -> ``card._revert_auto_score_failure()`` (back to
                          REVEALED_NOT_SCORED with ``auto_scoring_failed`` latched).
       - Whole-batch raise -> every card still in REVEALED_PENDING_AUTO_SCORE is reverted via
                              the same path.

    4. ``_handle_batched_auto_score`` post-loop: requeued cards are re-inserted in
       due-time order via ``_emplace_in_due_order_fixing_current`` and added to ``_remaining``
       (NOT ``_next_remaining`` — they are part of the round we just swapped in); failed
       cards stay in place but are added back to
       ``_remaining`` so the user can rate them manually. ``_autoscore_task`` is cleared,
       ``dirty`` is emitted, then ``_check_ready_to_autoscore`` and ``_check_done`` run in
       that order — the former picking up any cards the user deferred mid-batch (which can
       recursively dispatch a follow-up batch), the latter closing the session if every
       card is now terminally scored.

============================================================================================
Cursor management
============================================================================================

    - ``_current_card_index`` is an index into ``_cards``. ``_cards`` is mutated in place by
      a requeue's remove+insert (the requeued card lands in due-time order among the
      AWAITING_REVEAL cards from the cursor forward — applies to AGAIN and to HARD/GOOD on
      a card still inside the Learning ladder). See ``_emplace_in_due_order_fixing_current``
      for the cards-behind-cursor exception. The cursor is fixed up so it points at the
      same logical "next card" it would under a plain remove+append: a requeue of a
      middle/first card implicitly shifts a new card into the cursor position; a requeue
      of the last card keeps the cursor on the just-requeued card iff no later-due
      AWAITING_REVEAL sibling was found ahead of it.
    - ``_goto_next_unscored_card`` is the only place that walks the cursor between rounds.
      It prefers ``_remaining`` and falls back to ``_next_remaining`` so the cursor doesn't
      stall on a SCORED card when the only unscored cards are requeued and waiting.
    - The think-time timer is paused when leaving FRONT and unpaused when entering FRONT.
      ``_pause_current_if_front`` / ``_unpause_current_if_front`` are the FRONT-state guards;
      they make navigation onto a SCORED, REVEALED_*, or AWAITING_REVEAL card a no-op for the
      timer (the card's own state machine asserts FRONT inside ``pause`` / ``unpause``).
"""

import asyncio
from enum import Enum, auto
from typing import Any

from fsrs import Scheduler, State

from rhizome.db.operations.flashcards import commit_fsrs_card
from rhizome.logs import get_logger
from rhizome.app.flashcard_review.timer import Timer
from rhizome.app.flashcard_review.flashcard import Flashcard, FlashcardData
from rhizome.app.model import Emitter, ViewModelBase

_logger = get_logger("tui.flashcard_review_vm")


class FlashcardReviewModel(ViewModelBase):

    class State(Enum):
        START = auto()
        REVIEWING = auto()
        DONE = auto()

    def __init__(
        self,
        cards: list[FlashcardData],
        session_factory: Any,
        auto_score_enabled: bool = False,
        auto_scorer: Any = None,
        scheduler: Scheduler | None = None,
        auto_approve_auto_score: bool = False,
    ):
        super().__init__()
        # ``session_factory`` is held only for the public ``commit()`` API, which is never called
        # internally — no DB I/O happens during the session itself. FSRS state lives entirely in memory on
        # each Flashcard, mutated through ``self._scheduler``.
        self._session_factory = session_factory
        self._scheduler = scheduler if scheduler is not None else Scheduler()
        self._cards = [Flashcard(card, self._scheduler) for card in cards]
        self._current_card_index = 0

        self._auto_score_enabled = auto_score_enabled
        self._auto_scorer = auto_scorer
        # When True the batch auto-scorer applies its rating immediately (AUTO_ACCEPT
        # mode). When False each rating lands in SCORED_PENDING_APPROVAL awaiting the
        # user's approve/reject decision (REQUIRE_APPROVAL mode). Toggleable mid-session
        # via shift+tab; only affects cards the scorer hasn't yet processed (in-flight
        # batches are unaffected).
        self._auto_approve_auto_score = auto_approve_auto_score

        # Internal state
        self.state = FlashcardReviewModel.State.START
        self._cancelled = False
        self._collapsed = False
        self._help_visible = False
        self._timers_visible = False
        self._autoscore_task: asyncio.Task | None = None
        self._latest_message: str | None = None

        self._remaining_before_batched_autoscore = set(card.id for card in self._cards)
        self._next_remaining_before_batched_autoscore = set()

        # Bridge the async due-timer reveal (which happens inside Flashcard, not a VM method) into the
        # dirty emit. Async entry point — fires directly via self, not through any caller's emitter.
        for card in self._cards:
            card._on_due_reveal = lambda: self.emit(self.dirty)


    # ========================================================================================================================
    # Public API
    # ========================================================================================================================

    @property
    def current_card(self) -> Flashcard | None:
        if self.state == FlashcardReviewModel.State.START or not self._cards:
            return None
        return self._cards[self._current_card_index]

    @property
    def auto_score_active_for_current_card(self) -> bool:
        """Whether the auto-score path applies to the current card: auto-scoring is on and the
        card hasn't had it suppressed by a scorer failure or a user reject. Drives the enter-default
        on a REVEALED_NOT_SCORED card (defer-to-auto vs. manual good) and the rating-row label."""
        card = self.current_card
        return (
            self._auto_score_enabled
            and card is not None
            and not card.auto_scoring_failed
            and not card.auto_score_discarded
        )

    @property
    def cancelled(self) -> bool:
        return self._cancelled


    @property
    def collapsed(self) -> bool:
        return self._collapsed
    
    @collapsed.setter
    def collapsed(self, value: bool) -> None:
        if self._collapsed == value:
            return
        self._collapsed = value
        self.emit(self.dirty)


    @property
    def help_visible(self) -> bool:
        return self._help_visible

    @help_visible.setter
    def help_visible(self, value: bool) -> None:
        if self._help_visible == value:
            return
        self._help_visible = value
        self.emit(self.dirty)


    @property
    def timers_visible(self) -> bool:
        return self._timers_visible
    
    @timers_visible.setter
    def timers_visible(self, value: bool) -> None:
        if self._timers_visible == value:
            return
        self._timers_visible = value
        self.emit(self.dirty)


    @property
    def auto_score_enabled(self) -> bool:
        return self._auto_score_enabled

    @auto_score_enabled.setter
    def auto_score_enabled(self, value: bool) -> None:
        if self._auto_score_enabled == value:
            return
        self._auto_score_enabled = value
        self.emit(self.dirty)


    @property
    def auto_approve_auto_score(self) -> bool:
        return self._auto_approve_auto_score

    @auto_approve_auto_score.setter
    def auto_approve_auto_score(self, value: bool) -> None:
        if self._auto_approve_auto_score == value:
            return
        self._auto_approve_auto_score = value
        self.emit(self.dirty)


    def toggle_collapsed(self):
        assert self.state == FlashcardReviewModel.State.DONE
        self.collapsed = not self.collapsed

    def toggle_help_visible(self) -> None:
        self.help_visible = not self._help_visible

    def toggle_timers_visible(self) -> None:
        self.timers_visible = not self.timers_visible

    def toggle_auto_score_enabled(self) -> None:
        assert self.state == FlashcardReviewModel.State.REVIEWING
        self.auto_score_enabled = not self._auto_score_enabled

    def toggle_auto_approve_auto_score(self) -> None:
        assert self.state == FlashcardReviewModel.State.REVIEWING
        self.auto_approve_auto_score = not self._auto_approve_auto_score

    def toggle_flag_current_card(self) -> None:
        """Flip the user's "flag for later" annotation on the current card. Entirely orthogonal to card state.
        
        Surfaces in the result payload so callers can revisit flagged cards after the
        session."""
        assert self.state == FlashcardReviewModel.State.REVIEWING
        if self.current_card is None:
            return
        self.current_card.toggle_flagged()
        self.emit(self.dirty)


    def accept_all_auto_scores(self) -> None:
        """Approve every card currently in SCORED_PENDING_APPROVAL with its staged
        rating. No-op if there are no such cards.
        """
        assert self.state == FlashcardReviewModel.State.REVIEWING

        pending_approval = [
            c for c in self._cards
            if c.state == Flashcard.State.SCORED_PENDING_APPROVAL
        ]
        if not pending_approval:
            return

        for card in pending_approval:
            self._remaining_before_batched_autoscore.discard(card.id)
            self._next_remaining_before_batched_autoscore.discard(card.id)
            card.approve_pending_score()

            # Requeue if needed
            if card.fsrs_card.state in (State.Learning, State.Relearning):
                self._emplace_in_due_order_fixing_current(card)
                self._remaining_before_batched_autoscore.add(card.id)

        self._latest_message = f"Approved {len(pending_approval)} auto-scored card(s)"
        self._goto_next_unscored_card()
        with self.emit_once(self.dirty) as emitter:
            emitter.emit(self.dirty)
            self._check_ready_to_autoscore(emitter)
            self._check_done(emitter)


    @property
    def autoscore_in_progress(self) -> bool:
        return self._autoscore_task is not None and not self._autoscore_task.done()

    def pop_latest_message(self) -> str | None:
        """Read-and-clear the latest user-action message. Returns ``None`` if no
        message has been emitted since the last pop. View calls this on every
        refresh so that unrelated ``dirty`` emits don't re-surface a stale message."""
        msg = self._latest_message
        self._latest_message = None
        return msg

    @property
    def num_remaining(self) -> int:
        """Number of cards not yet in the terminal SCORED state — i.e. anything still
        needing user attention (REVEALED_*, SCORED_PENDING_APPROVAL, AWAITING_REVEAL,
        FRONT). Independent of the round-bookkeeping ``_remaining`` set, which only
        governs auto-score batch timing."""
        return sum(
            1 for c in self._cards if c.state != Flashcard.State.SCORED
        )

    @property
    def remaining_position(self) -> int | None:
        """1-indexed position of the current card among the not-yet-SCORED cards
        (ordered by their index in ``_cards``). ``None`` if the current card is itself
        in the SCORED state."""
        current = self.current_card
        if current is None or current.state == Flashcard.State.SCORED:
            return None
        pos = 0
        for card in self._cards:
            if card.state != Flashcard.State.SCORED:
                pos += 1
                if card is current:
                    return pos
        return None


    def begin(self):
        """Transition state from START to REVIEWING."""
        assert self.state == FlashcardReviewModel.State.START

        self.state = FlashcardReviewModel.State.REVIEWING
        # Kick off the first card's think-time timer.
        self._unpause_current_if_front()

        self.emit(self.dirty)


    def reveal_back_current_card(self) -> None:
        """Flip a FRONT card to REVEALED_NOT_SCORED. Driven by enter on FRONT."""
        assert self.state == FlashcardReviewModel.State.REVIEWING
        if self.current_card is None or self.current_card.state != Flashcard.State.FRONT:
            return
        self.current_card.reveal_back()
        self.emit(self.dirty)


    def reveal_front_current_card(self) -> None:
        """Flip an AWAITING_REVEAL card back to FRONT for re-rating. Driven by enter on AWAITING_REVEAL."""
        assert self.state == FlashcardReviewModel.State.REVIEWING
        if self.current_card is None or self.current_card.state != Flashcard.State.AWAITING_REVEAL:
            return
        self.current_card.reveal_front()
        self.emit(self.dirty)


    def advance_to_next_unscored(self) -> None:
        """Move the cursor to the next card needing attention. Driven by enter on
        SCORED / REVEALED_PENDING_AUTO_SCORE."""
        assert self.state == FlashcardReviewModel.State.REVIEWING
        if self.current_card is None:
            return
        self._goto_next_unscored_card()
        self.emit(self.dirty)


    def approve_pending_score(self) -> None:
        """Approve a staged auto-score by routing the proposed rating through the
        normal scoring path. Driven by enter on SCORED_PENDING_APPROVAL."""
        assert self.state == FlashcardReviewModel.State.REVIEWING
        card = self.current_card
        if card is None or card.state != Flashcard.State.SCORED_PENDING_APPROVAL:
            return
        pending = card.pending_score
        assert pending is not None
        self.score_current_card(pending)


    def reject_pending_score(self) -> None:
        """Discard a staged auto-score without applying a rating. Card returns to
        REVEALED_NOT_SCORED with ``auto_score_discarded`` latched (so the enter-
        default falls back to manual GOOD). Driven by 'd' on SCORED_PENDING_APPROVAL."""
        assert self.state == FlashcardReviewModel.State.REVIEWING
        card = self.current_card
        if card is None or card.state != Flashcard.State.SCORED_PENDING_APPROVAL:
            return
        card.discard_pending_score()
        self.emit(self.dirty)


    def cancel(self):
        """Transition to the cancelled DONE state."""
        assert self.state != FlashcardReviewModel.State.DONE
        self._cancelled = True
        self.finish()


    def finish(self, emitter: Emitter | None = None):
        """Transition to the DONE state.

        ``emitter`` participates in a caller's ``emit_once`` batch (e.g. ``_check_done`` called from
        within ``score_current_card``'s batch); when ``None`` the dirty emit fires immediately via
        ``self``.
        """
        assert self.state != FlashcardReviewModel.State.DONE

        if emitter is None:
            emitter = self

        # Pause the current card's timer if it's still running (the session is ending mid-think, e.g. on
        # ctrl+c). Must be done before state transition since Flashcard.pause() asserts state == FRONT.
        self._pause_current_if_front()

        self.state = FlashcardReviewModel.State.DONE

        # By default, start in the collapsed state
        self._collapsed = True

        # If we had an autoscore task running, cancel it.
        if self._autoscore_task is not None and not self._autoscore_task.done():
            self._autoscore_task.cancel()
            self._autoscore_task = None

        # Additionally, if any cards are still in the AWAITING_REVEAL state, we should transition them to the SCORED
        # state with a score of SKIPPED, since the session is effectively over and these cards won't be coming back around.
        for card in self._cards:
            if card.state == Flashcard.State.AWAITING_REVEAL:
                card.skip() # This will stop the due timer and reset the card state

        emitter.emit(self.dirty)


    def next_card(self):
        """Navigate to the next card, wrapping around if necessary. Does not change card state (other than pausing/unpausing the think-time timer)."""
        assert self.state != FlashcardReviewModel.State.START
        if not self._cards:
            return

        self._pause_current_if_front()
        self._step_card(1)
        self._unpause_current_if_front()
        self.emit(self.dirty)


    def prev_card(self):
        """Navigate to the previous card, wrapping around if necessary. Does not change card state (other than pausing/unpausing the think-time timer)."""
        assert self.state != FlashcardReviewModel.State.START
        if not self._cards:
            return

        self._pause_current_if_front()
        self._step_card(-1)
        self._unpause_current_if_front()
        self.emit(self.dirty)


    def score_current_card(self, score: Flashcard.Score):
        """Score the current card with the given score, transitioning card state accordingly."""
        assert self.state == FlashcardReviewModel.State.REVIEWING
        
        if self.current_card is None:
            return
        
        assert self.current_card.state in [
            Flashcard.State.REVEALED_NOT_SCORED,
            Flashcard.State.REVEALED_PENDING_AUTO_SCORE,
            Flashcard.State.SCORED_PENDING_APPROVAL,
        ]

        # We can reach this method for a card in three states:
        #   1) REVEALED_NOT_SCORED: user manually scored a card they just revealed
        #   2) REVEALED_PENDING_AUTO_SCORE: user manually scored a card that was pending auto-score (must
        #      guard against in-flight autoscore task — user's score would be overridden by the batch)
        #   3) SCORED_PENDING_APPROVAL: user is approving the staged auto-score rating; clear the pending
        #      slot before set_score routes the card forward into SCORED / AWAITING_REVEAL.

        if self.current_card.state == Flashcard.State.REVEALED_PENDING_AUTO_SCORE:
            if self.autoscore_in_progress:
                return

        if self.current_card.state == Flashcard.State.SCORED_PENDING_APPROVAL:
            self.current_card._pending_score = None

        # Discard from BOTH queues — a card transitioning to SCORED must not leave a ghost id in
        # _next_remaining (e.g. a previously-AGAIN'd card that the user revealed and scored before its due
        # timer fired). The requeue branch below re-adds to _next afterwards for cards that end up in
        # Learning / Relearning.
        self._remaining_before_batched_autoscore.discard(self.current_card.id)
        self._next_remaining_before_batched_autoscore.discard(self.current_card.id)

        # AUTO and SKIPPED don't run the rating through the scheduler, so there's no FSRS state to branch
        # on — they're terminal-for-now transitions handled directly.
        if score == Flashcard.Score.AUTO:
            self.current_card.set_score_auto()
            self._latest_message = "Card deferred to auto-scorer"
            self._goto_next_unscored_card()

        elif score == Flashcard.Score.SKIPPED:
            self.current_card.skip()
            self._latest_message = "Skipped card"
            self._goto_next_unscored_card()

        # EASY/GOOD/HARD/AGAIN: apply the rating, then branch on the post-rating FSRS state.
        #   - State.Review
        #       - graduated; card lands in SCORED; already removed from both queues above.
        #   - State.Learning/Relearning
        #       - still in the (re)learning step ladder card lands in AWAITING_REVEAL and
        #         must come back this session. Requeue in due-time order (see
        #         _emplace_in_due_order_fixing_current) and add to _next_remaining
        #         (swapped in once the current round drains).
        elif score in [
            Flashcard.Score.EASY,
            Flashcard.Score.GOOD,
            Flashcard.Score.HARD,
            Flashcard.Score.AGAIN,
        ]:
            current_card = self.current_card
            current_card.set_score(score)
            requeued = current_card.fsrs_card.state in (State.Learning, State.Relearning)

            if requeued:
                # Re-insert in due-time order among the AWAITING_REVEAL cards ahead of
                # the cursor. _emplace_in_due_order_fixing_current adjusts the cursor so
                # it continues to point at the same logical "next card" it would have
                # under the old append-to-back behavior.
                self._emplace_in_due_order_fixing_current(current_card)
                self._next_remaining_before_batched_autoscore.add(current_card.id)

            self._latest_message = f"Scored {score.name.lower()}" + (
                " — requeued for later review" if requeued else ""
            )

            # Land on the next unscored card. If the implicit shift above already placed us on an unscored
            # card, _goto will stay put and just start that card's timer.
            self._goto_next_unscored_card()

        # Batch the closing emit + checks: _check_ready_to_autoscore and _check_done can each
        # trigger their own emits (transition into a batch dispatch, or call finish()), and we
        # want the view to see this whole user action as a single repaint. Order still matters:
        # if a batch is dispatched, _check_done correctly stays its hand because the pending
        # cards aren't terminally scored yet.
        with self.emit_once(self.dirty) as emitter:
            emitter.emit(self.dirty)
            self._check_ready_to_autoscore(emitter)
            self._check_done(emitter)


    def reset_current_card(self):
        """Reset the current card, transitioning card and VM state accordingly."""
        assert self.state == FlashcardReviewModel.State.REVIEWING

        if not self.current_card:
            return

        # Check if an autoscore is in progress which includes this card. If so, we should disallow resetting.
        if self.autoscore_in_progress and self.current_card.state == Flashcard.State.REVEALED_PENDING_AUTO_SCORE:
            return

        self.current_card.reset()
        self.current_card.unpause() # Restart the timer

        # If the card was previously scored, we need to add it back to the remaining queue. If it was already in the remaining queue, no-op.
        self._remaining_before_batched_autoscore.add(self.current_card.id)
        self._next_remaining_before_batched_autoscore.discard(self.current_card.id)

        self._latest_message = "Reset card"
        with self.emit_once(self.dirty) as emitter:
            emitter.emit(self.dirty)
            self._check_ready_to_autoscore(emitter)
            self._check_done(emitter)


    def toggle_skip_current_card(self):
        """Skip/unskip the current card, transitioning card and VM state accordingly."""
        assert self.state == FlashcardReviewModel.State.REVIEWING

        if not self.current_card:
            return
        
        # First, we check if the card is in the AWAITING_REVEAL state. If so, we ignore it. Skipping cards that are
        # awaiting revealed is done purely out of convenience, as "resetting" a card which was previously awaiting reveal,
        # but was then skipped, is a bit tricky to get right.
        if self.current_card.state == Flashcard.State.AWAITING_REVEAL:
            return
        
        # Next, we check if the card was already scored as SKIPPED. If so, we "unskip" it by resetting the card state, 
        # and adding it back to the remaining queue if necessary.
        #
        # Remark: we _don't_ need to guard against an in-flight autoscore task in this case because if the card was skipped,
        # then it DEFINITELY didn't end up in the autoscore batch.
        if self.current_card.scored and self.current_card.score == Flashcard.Score.SKIPPED:
            self.current_card.reset()
            self.current_card.unpause() # Restart the timer

            # Re-add to remaining if not already there - remove from next round remaining just in case
            self._remaining_before_batched_autoscore.add(self.current_card.id)
            self._next_remaining_before_batched_autoscore.discard(self.current_card.id)

        # If the card wasn't already skipped, we skip it by setting score to SKIPPED and transitioning state,
        # removing from remaining queue if necessary. Skipping is only possible for cards that _aren't_ scored.
        #
        # Remark: here, we _also_ don't need to guard against in-flight autoscore tasks, since a card in either of these
        # states is also DEFINITELY not in the autoscore batch.
        elif self.current_card.state in [
            Flashcard.State.FRONT,
            Flashcard.State.REVEALED_NOT_SCORED
        ]:
            self.current_card.skip()

            # Remove from both current and next round of remaining
            self._remaining_before_batched_autoscore.discard(self.current_card.id)
            self._next_remaining_before_batched_autoscore.discard(self.current_card.id)

        # SCORED_PENDING_APPROVAL: discard the staged auto-score (lands in REVEALED_NOT_SCORED) and
        # then skip from there. Same queue treatment as the FRONT / REVEALED_NOT_SCORED branch above.
        elif self.current_card.state == Flashcard.State.SCORED_PENDING_APPROVAL:
            self.current_card.discard_pending_score()
            self.current_card.skip()

            self._remaining_before_batched_autoscore.discard(self.current_card.id)
            self._next_remaining_before_batched_autoscore.discard(self.current_card.id)

        else:
            return # Nothing to do

        self._latest_message = (
            "Skipped card" if self.current_card.score == Flashcard.Score.SKIPPED else "Unskipped card"
        )
        with self.emit_once(self.dirty) as emitter:
            emitter.emit(self.dirty)
            self._check_ready_to_autoscore(emitter)
            self._check_done(emitter)



    # ========================================================================================================================
    # Private Helpers
    # ========================================================================================================================

    def _step_card(self, step: int):
        self._current_card_index += step

        if self._current_card_index < 0:
            self._current_card_index = len(self._cards) - 1

        elif self._current_card_index >= len(self._cards):
            self._current_card_index = 0

    def _pause_current_if_front(self):
        """Pause the current card's think-time timer if it's in FRONT."""
        if self.current_card and self.current_card.state == Flashcard.State.FRONT:
            self.current_card.pause()

    def _unpause_current_if_front(self):
        """Unpause the current card's think-time timer if it's in FRONT."""
        if self.current_card and self.current_card.state == Flashcard.State.FRONT:
            self.current_card.unpause()

    def _emplace_in_due_order_fixing_current(self, card):
        """Re-insert a just-requeued AWAITING_REVEAL card in due-time order among the
        AWAITING_REVEAL cards from the cursor forward.

        Walks ``_cards`` from ``_current_card_index`` (inclusive) and finds the first
        AWAITING_REVEAL card whose ``due_in`` is greater than ``card.due_in``; inserts
        immediately before it. Non-AWAITING_REVEAL cards are transparent to the scan.
        Falls back to appending at the end if no such card is found.

        Cards behind the cursor are intentionally not consulted — the user has moved
        past them, and yanking them back into the visited-forward path to maintain a
        global ordering would be more disruptive than the local out-of-order. As a
        corner case, the append fallback may land the card after a later-due
        AWAITING_REVEAL sibling that lives behind the cursor; accepted under the same
        trade-off.

        Cursor handling matches the old remove+append behavior: tracks the same logical
        card the cursor was on pre-pop, EXCEPT when the cursor was on the popped card
        itself — then it preserves the cursor's numeric position, so a requeue of the
        last card leaves the cursor on the just-requeued card (the requeue brings the
        cursor back in-bounds at the same index), and a requeue of an earlier card
        implicitly shifts a new card into the vacated cursor position.
        """
        pos = self._cards.index(card)
        cursor_was_on_card = pos == self._current_card_index
        self._cards.pop(pos)
        if pos < self._current_card_index:
            self._current_card_index -= 1

        insert_at = len(self._cards)
        for i in range(self._current_card_index, len(self._cards)):
            other = self._cards[i]
            if (
                other.state == Flashcard.State.AWAITING_REVEAL
                and other.due_in > card.due_in
            ):
                insert_at = i
                break

        self._cards.insert(insert_at, card)
        # Only bump when cursor is tracking a logical card AND that card just shifted
        # right by the insert. When cursor was on the popped card, there's no logical
        # card to follow — leave cursor at its numeric position (the insert may have
        # just made it valid again in the popped-last case).
        if not cursor_was_on_card and insert_at <= self._current_card_index:
            self._current_card_index += 1

    def _goto_next_unscored_card(self):
        """Land the cursor on the next card still needing attention.

        Prefers cards in the current round's ``_remaining`` set; falls back to ``_next_remaining`` if the
        current round is drained. The fallback matters when e.g. the first card was AGAIN'd and the rest of
        the round was completed — without it, the cursor would stall on the just-scored last card instead
        of landing on the AGAIN'd card (which is now in AWAITING_REVEAL, waiting for its due timer).

        - If the current card is already in the preferred set, stays put.
        - Otherwise, walks forward cyclically until a matching card is found. If the first pass
          (``_remaining``) yields nothing, tries ``_next_remaining`` before giving up.

        Manages the think-time timer across the move: the outgoing card is paused (if in FRONT) and the
        landing card is unpaused (if in FRONT). The unpause fires even when we stay put, because callers
        may invoke this after an implicit cursor shift (e.g. the AGAIN ``remove + append`` that promotes a
        new card into the cursor's position) where the landing card's timer hasn't been started yet.
        """
        if not self._cards:
            return

        for target_set in (
            self._remaining_before_batched_autoscore,
            self._next_remaining_before_batched_autoscore,
        ):
            if not target_set:
                continue

            if self.current_card.id in target_set:
                # Stay put, but make sure the timer is running for the landing card (it may have been
                # implicitly shifted into place via the AGAIN remove+append).
                self._unpause_current_if_front()
                return

            self._pause_current_if_front()
            original_index = self._current_card_index
            while True:
                self._step_card(1)
                if self.current_card.id in target_set:
                    self._unpause_current_if_front()
                    return
                if self._current_card_index == original_index:
                    # Full loop, nothing in this set — fall through to the next target_set (or exit entirely).
                    break

        # Both sets empty or exhausted — leave cursor where it is and make sure timer state is consistent.
        self._unpause_current_if_front()

    def _check_ready_to_autoscore(self, emitter: Emitter | None = None):
        """If the current round has drained and there are cards waiting on auto-scoring, swap in the next
        round and dispatch a batch.

        Round rollover is intentionally bundled here: by the time ``_remaining`` drains, every card the
        user has ratings-of-record for in this round either landed in SCORED (terminal) or in
        REVEALED_PENDING_AUTO_SCORE (waiting for the batch). The ``_next_remaining`` carry-over is the
        right input for the next round.

        ``emitter`` participates in a caller's ``emit_once`` batch; when ``None`` the dirty emit fires
        immediately via ``self``.
        """
        if emitter is None:
            emitter = self

        if self._remaining_before_batched_autoscore:
            return

        # Guard against re-entry while a batch is already running. The running batch's completion handler
        # will call back into this method once it's done.
        if self.autoscore_in_progress:
            return

        # Round rollover: AGAIN'd / requeued cards from this round become the next round's queue.
        self._remaining_before_batched_autoscore = self._next_remaining_before_batched_autoscore
        self._next_remaining_before_batched_autoscore = set()

        pending_auto_score = [c for c in self._cards if c.score == Flashcard.Score.AUTO]
        if not pending_auto_score:
            return

        self._autoscore_task = asyncio.create_task(self._handle_batched_auto_score(pending_auto_score))
        emitter.emit(self.dirty)

    def _check_done(self, emitter: Emitter | None = None):
        """If every card is terminally scored (HARD/GOOD/EASY/SKIPPED in the SCORED state), transition to
        DONE.

        Pending AUTO cards (state == REVEALED_PENDING_AUTO_SCORE) and AWAITING_REVEAL cards both fail this
        check on their state alone, so no separate ``autoscore_in_progress`` guard is needed — by the time
        the last batch completes and its requeued/failed cards are placed, this check runs and either
        fires or doesn't on its own merits.
        """
        terminal_scores = {
            Flashcard.Score.HARD,
            Flashcard.Score.GOOD,
            Flashcard.Score.EASY,
            Flashcard.Score.SKIPPED,
        }
        if all(
            c.state == Flashcard.State.SCORED and c.score in terminal_scores
            for c in self._cards
        ):
            self.finish(emitter=emitter)


    async def _handle_batched_auto_score(self, pending_auto_score: list[Flashcard]) -> None:
        try:
            requeued, failed, pending_approval = await self._auto_score(pending_auto_score)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _logger.warning("Batch auto-scoring failed: %s", exc)
            # Whole-batch failure: revert every card that's still sitting in REVEALED_PENDING_AUTO_SCORE
            # (i.e. the scorer didn't get to produce a rating for it before blowing up). Anything that
            # already made it through ``set_score`` / ``set_pending_score`` is already committed.
            requeued = []
            failed = []
            pending_approval = []
            for card in pending_auto_score:
                if card.state == Flashcard.State.REVEALED_PENDING_AUTO_SCORE:
                    card._revert_auto_score_failure()
                    failed.append(card)

        # Requeued cards (auto-accept mode, post-rating FSRS state in Learning/Relearning) are
        # re-inserted in due-time order among the AWAITING_REVEAL cards ahead of the cursor.
        for card in requeued:
            self._emplace_in_due_order_fixing_current(card)
            self._remaining_before_batched_autoscore.add(card.id)

        # Failed and pending-approval cards stay in place positionally. Failed cards need manual rating;
        # pending-approval cards need user approval or discard. Either way, the round can't complete until
        # the user attends to them, so they belong in _remaining.
        for card in failed:
            self._remaining_before_batched_autoscore.add(card.id)
        for card in pending_approval:
            self._remaining_before_batched_autoscore.add(card.id)

        # The cursor may be sitting on a card that's now SCORED; advance it to the next unscored card (if
        # any).
        self._goto_next_unscored_card()

        # Clear the task handle _before_ any follow-up calls that might observe ``autoscore_in_progress``
        # or try to cancel us.
        self._autoscore_task = None

        scored_n = len(requeued) + len(pending_approval)
        self._latest_message = f"Auto-scorer finished — {scored_n} scored" + (
            f", {len(failed)} failed" if failed else ""
        )
        # Pick up anything the user drained while the batch was running (may dispatch a follow-up batch),
        # then close the session if every card is now terminally scored. Batch the emits so the view
        # sees a single repaint covering the batch result + any cascading state changes.
        with self.emit_once(self.dirty) as emitter:
            emitter.emit(self.dirty)
            self._check_ready_to_autoscore(emitter)
            self._check_done(emitter)


    async def _auto_score(
        self, pending_auto_score: list[Flashcard]
    ) -> tuple[list[Flashcard], list[Flashcard], list[Flashcard]]:
        """Batch-score every pending-auto card via the scorer subagent.

        For each card, stages the rating returned by the scorer for user approval via ``set_pending_score``
        (currently always REQUIRE_APPROVAL behavior — the AutoScoreMode toggle that picks AUTO_ACCEPT vs
        REQUIRE_APPROVAL comes in a follow-up step). Every successful rating, including AGAIN, lands in
        SCORED_PENDING_APPROVAL — the user has the same say over a "model wants to see this again" call as
        a "model thinks you got it" call. No FSRS state advances and no DB commits.

        Returns a tuple ``(failed_cards, pending_approval_cards)``:

        - ``failed_cards``: cards the scorer couldn't score (dropped from the response, non-integer, or
          out-of-range). Reverted via ``Flashcard._revert_auto_score_failure`` — now ``REVEALED_NOT_SCORED``
          with ``auto_scoring_failed == True`` and need to go back into ``_remaining`` for manual rating.

        - ``pending_approval_cards``: successfully-rated cards now sitting in SCORED_PENDING_APPROVAL.
          Caller adds these to ``_remaining`` so the round stays open until the user approves or discards
          each one.

        Uses the stable flashcard id as ``flashcard_id`` in the prompt so results map back unambiguously,
        regardless of return order.
        """
        if self._auto_scorer is None:
            raise RuntimeError("auto-score invoked without a configured scorer subagent")

        prompt_parts = ["Score the following flashcard answers:\n\n"]
        for card in pending_auto_score:
            prompt_parts.append(f"Flashcard {card.id}:\n")
            prompt_parts.append(f"  Question: {card.question}\n")
            prompt_parts.append(f"  Expected answer: {card.answer}\n")
            prompt_parts.append(
                f"  User's answer: {card.user_answer or '(blank)'}\n"
            )
            prompt_parts.append(
                f"  Time spent: {round(card.elapsed_time, 1)}s\n"
            )
            if card.testing_notes:
                prompt_parts.append(f"  Testing notes: {card.testing_notes}\n")
            prompt_parts.append("\n")

        await self._auto_scorer.ainvoke("".join(prompt_parts))
        parsed = self._auto_scorer.structured_response
        if parsed is None or not parsed.results:
            raise RuntimeError("scorer returned no structured result")

        results_by_id: dict[int, Any] = {}
        for r in parsed.results:
            try:
                results_by_id[int(r.flashcard_id)] = r
            except (AttributeError, ValueError, TypeError):
                continue

        score_map = {
            1: Flashcard.Score.AGAIN,
            2: Flashcard.Score.HARD,
            3: Flashcard.Score.GOOD,
            4: Flashcard.Score.EASY,
        }

        requeued_cards: list[Flashcard] = []
        failed_cards: list[Flashcard] = []
        pending_approval_cards: list[Flashcard] = []

        def _fail(card: Flashcard, reason: str) -> None:
            _logger.warning("%s for flashcard id=%d", reason, card.id)
            card._revert_auto_score_failure()
            failed_cards.append(card)

        for card in pending_auto_score:
            result = results_by_id.get(card.id)
            if result is None:
                _fail(card, "Scorer returned no result")
                continue

            try:
                rating = int(result.score)
            except (AttributeError, ValueError, TypeError):
                _fail(card, "Scorer returned non-integer score")
                continue
            if rating not in score_map:
                _fail(card, f"Scorer returned out-of-range score {rating!r}")
                continue

            if self._auto_approve_auto_score:
                card.set_score(score_map[rating])
                if card.fsrs_card.state in (State.Learning, State.Relearning):
                    requeued_cards.append(card)
            else:
                card.set_pending_score(score_map[rating])
                pending_approval_cards.append(card)

        return requeued_cards, failed_cards, pending_approval_cards


    async def commit(self) -> None:
        """Persist every card's current FSRS scheduling state to the DB.

        Idempotent — calling this repeatedly writes the same state each time (assuming no further ratings
        happen in between). Never called internally; reserved for the widget's caller (typically the
        ``review_present_flashcards`` tool) to invoke when a session completes against a non-ephemeral
        review session.

        Cards that haven't been rated this session (SKIPPED cards left in FRONT, untouched cards from a
        cancelled session) still get their FSRS state written — but since ``_current_fsrs_card`` was never
        advanced for those, the write is a no-op equivalent.
        """
        async with self._session_factory() as session:
            for card in self._cards:
                await commit_fsrs_card(
                    session, card.id, card._current_fsrs_card,
                )
            await session.commit()