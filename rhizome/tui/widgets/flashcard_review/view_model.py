"""View-model for the FlashcardReview widget.

This module owns the session-level state machine ``FlashcardReviewViewModel`` over a list of
``Flashcard`` instances (per-card state machine documented in ``flashcard.py``). The view
(``FlashcardReview``) subscribes to a single ``dirty`` observer list on the VM and re-renders
on every emit; it never mutates VM state directly.

See ``flashcard.py`` for the Flashcard state machine and FSRS state ownership docs.

============================================================================================
FlashcardReviewViewModel state machine
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
        -> DONE [via toggle_collapsed()]
            - The user issued the toggle-collapsed action from the done screen.
            - View-only state change: flips the ``_collapsed`` flag so the view collapses or
              re-expands the card detail panel. Does not affect the session outcome.

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

    4. ``_handle_batched_auto_score`` post-loop: requeued cards are moved to the back of
       ``_cards`` and added to ``_remaining`` (NOT ``_next_remaining`` — they are part of the
       round we just swapped in); failed cards stay in place but are added back to
       ``_remaining`` so the user can rate them manually. ``_autoscore_task`` is cleared,
       ``dirty`` is emitted, then ``_check_ready_to_autoscore`` and ``_check_done`` run in
       that order — the former picking up any cards the user deferred mid-batch (which can
       recursively dispatch a follow-up batch), the latter closing the session if every
       card is now terminally scored.

============================================================================================
Cursor management
============================================================================================

    - ``_current_card_index`` is an index into ``_cards``. ``_cards`` is mutated in place by
      a requeue's remove+append (so a requeued card always lands at the end of the display
      order — applies to AGAIN and to HARD/GOOD on a card still inside the Learning ladder).
      The cursor's *index* is preserved across this; this means after a requeue of a
      middle/first card, the cursor implicitly shifts to a new card (the one that slid into
      the vacated position), and after a requeue of the last card, the cursor stays on the
      just-requeued card.
    - ``_goto_next_unscored_card`` is the only place that walks the cursor between rounds.
      It prefers ``_remaining`` and falls back to ``_next_remaining`` so the cursor doesn't
      stall on a SCORED card when the only unscored cards are requeued and waiting.
    - The think-time timer is paused when leaving FRONT and unpaused when entering FRONT.
      ``_pause_current_if_front`` / ``_unpause_current_if_front`` are the FRONT-state guards;
      they make navigation onto a SCORED, REVEALED_*, or AWAITING_REVEAL card a no-op for the
      timer (the card's own state machine asserts FRONT inside ``pause`` / ``unpause``).
"""

import asyncio
from collections.abc import Callable
from enum import Enum, auto
from typing import Any

from fsrs import Scheduler, State
from textual import events

from rhizome.db.operations.flashcards import commit_fsrs_card
from rhizome.logs import get_logger
from rhizome.tui.widgets.flashcard_review._timer import Timer
from rhizome.tui.widgets.flashcard_review.flashcard import Flashcard, FlashcardData

_logger = get_logger("tui.flashcard_review_vm")


class Action(Enum):
    """Semantic keyboard actions handled by the view-model.

    Multiple actions can map to the same key (e.g. all four ``enter`` flavors in REVIEWING — REVEAL_BACK /
    REVEAL_FRONT / SCORE_DEFAULT / ADVANCE_NEXT — share ``"enter"``); the dispatcher disambiguates by state.
    Keeping them as separate Action members lets a help dropdown show contextual labels per state.
    """
    # START
    BEGIN = auto()             # enter

    # Cross-state navigation / lifecycle
    CANCEL = auto()            # ctrl+c (START + REVIEWING)
    PREV_CARD = auto()         # alt+left  (REVIEWING + DONE)
    NEXT_CARD = auto()         # alt+right (REVIEWING + DONE)

    # REVIEWING
    REVEAL_BACK = auto()       # enter on FRONT
    REVEAL_FRONT = auto()      # enter on AWAITING_REVEAL
    SCORE_DEFAULT = auto()     # enter on REVEALED_NOT_SCORED
    ADVANCE_NEXT = auto()      # enter on SCORED / PENDING_AUTO_SCORE
    SCORE_AGAIN = auto()       # 1
    SCORE_HARD = auto()        # 2
    SCORE_GOOD = auto()        # 3
    SCORE_EASY = auto()        # 4
    TOGGLE_TIMER = auto()      # ctrl+k
    RESET_CARD = auto()        # alt+x
    TOGGLE_SKIP = auto()       # alt+s
    APPROVE_AUTO_SCORE = auto()  # enter on SCORED_PENDING_APPROVAL
    REJECT_AUTO_SCORE = auto()   # d   on SCORED_PENDING_APPROVAL

    # DONE
    TOGGLE_COLLAPSED = auto()  # enter

    # Cross-state UI toggle
    TOGGLE_HELP = auto()       # alt+h
    TOGGLE_AUTO_SCORE = auto() # alt+a
    TOGGLE_AUTO_APPROVE_AUTO_SCORE = auto()  # shift+tab

    # Bulk action
    ACCEPT_ALL_AUTO_SCORES = auto()  # ctrl+enter (alias: ctrl+j)


KEYBINDINGS: dict[Action, str] = {
    Action.BEGIN: "enter",
    Action.CANCEL: "ctrl+c",
    Action.PREV_CARD: "alt+left",
    Action.NEXT_CARD: "alt+right",
    Action.REVEAL_BACK: "enter",
    Action.REVEAL_FRONT: "enter",
    Action.SCORE_DEFAULT: "enter",
    Action.ADVANCE_NEXT: "enter",
    Action.SCORE_AGAIN: "1",
    Action.SCORE_HARD: "2",
    Action.SCORE_GOOD: "3",
    Action.SCORE_EASY: "4",
    Action.TOGGLE_TIMER: "ctrl+k",
    Action.RESET_CARD: "alt+x",
    Action.TOGGLE_SKIP: "alt+s",
    Action.APPROVE_AUTO_SCORE: "enter",
    Action.REJECT_AUTO_SCORE: "d",
    Action.TOGGLE_COLLAPSED: "enter",
    Action.TOGGLE_HELP: "alt+h",
    Action.TOGGLE_AUTO_SCORE: "alt+a",
    Action.TOGGLE_AUTO_APPROVE_AUTO_SCORE: "shift+tab",
    Action.ACCEPT_ALL_AUTO_SCORES: "ctrl+enter",
}


# Textual reports certain Ctrl+<letter> keypresses as their underlying control-character
# names — most notably ``ctrl+enter`` arrives as ``ctrl+j``. Keep KEYBINDINGS using the
# user-friendly form (so the UI displays "ctrl+enter") and resolve aliases here when
# matching against ``event.key``.
_KEY_ALIASES: dict[str, str] = {
    "ctrl+j": "ctrl+enter",
}


def _matches_binding(event_key: str, binding: str) -> bool:
    """True iff ``event_key`` matches ``binding`` directly or via a known control-char
    alias (see ``_KEY_ALIASES``). Use in place of a raw ``==`` whenever the binding
    string might use a display-friendly form like ``ctrl+enter``."""
    return event_key == binding or _KEY_ALIASES.get(event_key) == binding


# Map of digit-key → score for the REVIEWING digit dispatch.
def _score_key_map() -> dict[str, "Flashcard.Score"]:
    return {
        KEYBINDINGS[Action.SCORE_AGAIN]: Flashcard.Score.AGAIN,
        KEYBINDINGS[Action.SCORE_HARD]: Flashcard.Score.HARD,
        KEYBINDINGS[Action.SCORE_GOOD]: Flashcard.Score.GOOD,
        KEYBINDINGS[Action.SCORE_EASY]: Flashcard.Score.EASY,
    }


class FlashcardReviewViewModel:

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
        self.state = FlashcardReviewViewModel.State.START
        self._cancelled = False
        self._collapsed = False
        self._help_visible = False
        self._autoscore_task: asyncio.Task | None = None

        self._remaining_before_batched_autoscore = set(card.id for card in self._cards)
        self._next_remaining_before_batched_autoscore = set()

        # Single "something changed" observer list. Views subscribe a render method that reads the whole
        # VM each time. Fired once per public transition method.
        self.dirty: list[Callable[[], None]] = []

        # Bridge the async due-timer reveal (which happens inside Flashcard, not a VM method) into the
        # dirty emit.
        for card in self._cards:
            card._on_due_reveal = lambda: self._emit(self.dirty)

    def _emit(self, listeners: list[Callable[[], None]]) -> None:
        for listener in listeners:
            listener()

    
    def on_key(self, event: events.Key) -> None:

        _logger.info("on_key: state=%s key=%r", self.state.name, event.key)

        # Cross-state: help toggle works in any state.
        if event.key == KEYBINDINGS[Action.TOGGLE_HELP]:
            self.toggle_help_visible()
            return

        # Cross-state: enter-default toggle (auto vs good).
        if event.key == KEYBINDINGS[Action.TOGGLE_AUTO_SCORE]:
            self.toggle_auto_score_enabled()
            return

        match self.state:
            case FlashcardReviewViewModel.State.START:
                self._on_key_start(event)
            case FlashcardReviewViewModel.State.REVIEWING:
                self._on_key_reviewing(event)
            case FlashcardReviewViewModel.State.DONE:
                self._on_key_done(event)


    def _on_key_start(self, event: events.Key) -> None:
        assert self.state == FlashcardReviewViewModel.State.START

        if event.key == KEYBINDINGS[Action.BEGIN]:
            self.begin()
        elif event.key == KEYBINDINGS[Action.CANCEL]:
            self.cancel()
            event.stop()

    def _on_key_reviewing(self, event: events.Key) -> None:
        assert self.state == FlashcardReviewViewModel.State.REVIEWING

        if event.key == KEYBINDINGS[Action.PREV_CARD]:
            self.prev_card()

        elif event.key == KEYBINDINGS[Action.NEXT_CARD]:
            self.next_card()

        elif event.key == KEYBINDINGS[Action.CANCEL]:
            self.cancel()
            event.stop()

        elif event.key == KEYBINDINGS[Action.TOGGLE_TIMER]:
            if self.current_card:
                self.current_card.toggle_timer_visible()
                self._emit(self.dirty)

        elif event.key == KEYBINDINGS[Action.TOGGLE_AUTO_APPROVE_AUTO_SCORE]:
            self.toggle_auto_approve_auto_score()
            event.stop()

        elif _matches_binding(event.key, KEYBINDINGS[Action.ACCEPT_ALL_AUTO_SCORES]):
            self.accept_all_auto_scores()
            event.stop()

        elif event.key == KEYBINDINGS[Action.RESET_CARD]:
            _logger.info(
                "alt+x: current_card=%s card_state=%s autoscore_in_progress=%s",
                self.current_card.id if self.current_card else None,
                self.current_card.state.name if self.current_card else None,
                self.autoscore_in_progress,
            )
            if self.current_card:

                # Check if an autoscore is in progress which includes this card. If so, we should disallow resetting.
                if self.autoscore_in_progress and self.current_card.state == Flashcard.State.REVEALED_PENDING_AUTO_SCORE:
                    _logger.info("alt+x: blocked by in-flight autoscore")
                    return

                _logger.info("alt+x: resetting card %s", self.current_card.id)

                self.current_card.reset()
                self.current_card.unpause() # Restart the timer

                # If the card was previously scored, we need to add it back to the remaining queue. If it was already in the remaining queue, no-op.
                self._next_remaining_before_batched_autoscore.discard(self.current_card.id)
                self._remaining_before_batched_autoscore.add(self.current_card.id)
                self._emit(self.dirty)
                self._check_ready_to_autoscore()
                self._check_done()

        elif event.key == KEYBINDINGS[Action.TOGGLE_SKIP]:
            _logger.info(
                "alt+s: current_card=%s card_state=%s scored=%s score=%s",
                self.current_card.id if self.current_card else None,
                self.current_card.state.name if self.current_card else None,
                self.current_card.scored if self.current_card else None,
                self.current_card.score.name if self.current_card and self.current_card.score else None,
            )
            if self.current_card:
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

                    # Re-add to remaining if not already there - remove from next round remaining just in case
                    self._remaining_before_batched_autoscore.add(self.current_card.id)
                    self._next_remaining_before_batched_autoscore.discard(self.current_card.id)
                    self._emit(self.dirty)
                    self._check_ready_to_autoscore()
                    self._check_done()

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
                    self._emit(self.dirty)
                    self._check_ready_to_autoscore()
                    self._check_done()

                # SCORED_PENDING_APPROVAL: discard the staged auto-score (lands in REVEALED_NOT_SCORED) and
                # then skip from there. Same queue treatment as the FRONT / REVEALED_NOT_SCORED branch above.
                elif self.current_card.state == Flashcard.State.SCORED_PENDING_APPROVAL:
                    self.current_card.discard_pending_score()
                    self.current_card.skip()

                    self._remaining_before_batched_autoscore.discard(self.current_card.id)
                    self._next_remaining_before_batched_autoscore.discard(self.current_card.id)
                    self._emit(self.dirty)
                    self._check_ready_to_autoscore()
                    self._check_done()


        elif event.key in (score_keys := _score_key_map()):
            if self.current_card and self.current_card.state in [
                Flashcard.State.REVEALED_NOT_SCORED,
                Flashcard.State.REVEALED_PENDING_AUTO_SCORE,
                Flashcard.State.SCORED_PENDING_APPROVAL,
            ]:
                # SCORED_PENDING_APPROVAL: digit acts as "reject the staged auto-score and apply the
                # manual rating instead". score_current_card handles clearing _pending_score for this state.
                self.score_current_card(score_keys[event.key])

        elif event.key == KEYBINDINGS[Action.REJECT_AUTO_SCORE]:
            # Reject the staged auto-score without applying a rating. Card goes back to REVEALED_NOT_SCORED
            # (with _auto_score_discarded latched so the enter-default falls back to manual GOOD).
            if self.current_card and self.current_card.state == Flashcard.State.SCORED_PENDING_APPROVAL:
                self.current_card.discard_pending_score()
                self._emit(self.dirty)

        # All four "enter" flavors in REVIEWING share the same key — keyed off SCORE_DEFAULT here for the
        # lookup; siblings REVEAL_BACK, REVEAL_FRONT, ADVANCE_NEXT all map to the same key. The branch
        # below disambiguates by card state.
        elif event.key == KEYBINDINGS[Action.SCORE_DEFAULT]:
            if self.current_card:
                if self.current_card.state == Flashcard.State.FRONT:
                    self.current_card.reveal_back()
                    self._emit(self.dirty)
                elif self.current_card.state == Flashcard.State.AWAITING_REVEAL:
                    self.current_card.reveal_front()
                    self._emit(self.dirty)

                elif self.current_card.state == Flashcard.State.REVEALED_NOT_SCORED:
                    # Default enter-on-revealed action depends on config and on whether this card has
                    # already failed auto-scoring.
                    if (
                        self._auto_score_enabled
                        and not self.current_card.auto_scoring_failed
                        and not self.current_card.auto_score_discarded
                    ):
                        self.score_current_card(Flashcard.Score.AUTO)
                    else:
                        # Either auto-scoring is disabled, or this card has already burned its auto-score
                        # attempt — user needs to rate manually. Default to GOOD.
                        self.score_current_card(Flashcard.Score.GOOD)

                elif self.current_card.state == Flashcard.State.SCORED_PENDING_APPROVAL:
                    # Approve the staged auto-score — apply it via the normal scoring path. The pending
                    # score is the rating the auto-scorer proposed.
                    pending = self.current_card.pending_score
                    assert pending is not None
                    self.score_current_card(pending)

                elif self.current_card.state in [
                    Flashcard.State.SCORED,
                    Flashcard.State.REVEALED_PENDING_AUTO_SCORE,
                ]:
                    # Card is already done (or queued for auto-score) — enter advances to the next card
                    # still needing attention.
                    self._goto_next_unscored_card()
                    self._emit(self.dirty)

            
    def _on_key_done(self, event: events.Key) -> None:
        # DONE
        #   - enter     -> collapse/expanded
        #   - alt+left (expanded) -> prev card
        #   - alt+right (expanded) -> next card
        assert self.state == FlashcardReviewViewModel.State.DONE

        if event.key == KEYBINDINGS[Action.PREV_CARD]:
            self.prev_card()

        elif event.key == KEYBINDINGS[Action.NEXT_CARD]:
            self.next_card()

        elif event.key == KEYBINDINGS[Action.TOGGLE_COLLAPSED]:
            self.toggle_collapsed()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def current_card(self) -> Flashcard | None:
        if self.state == FlashcardReviewViewModel.State.START or not self._cards:
            return None
        return self._cards[self._current_card_index]
    
    @property
    def cancelled(self) -> bool:
        return self._cancelled
    
    @property
    def collapsed(self) -> bool:
        return self._collapsed

    @property
    def help_visible(self) -> bool:
        return self._help_visible

    @help_visible.setter
    def help_visible(self, value: bool) -> None:
        if self._help_visible == value:
            return
        self._help_visible = value
        self._emit(self.dirty)

    def toggle_help_visible(self) -> None:
        self.help_visible = not self._help_visible

    @property
    def auto_score_enabled(self) -> bool:
        return self._auto_score_enabled

    @auto_score_enabled.setter
    def auto_score_enabled(self, value: bool) -> None:
        if self._auto_score_enabled == value:
            return
        self._auto_score_enabled = value
        self._emit(self.dirty)

    def toggle_auto_score_enabled(self) -> None:
        self.auto_score_enabled = not self._auto_score_enabled

    @property
    def auto_approve_auto_score(self) -> bool:
        """Auto-score acceptance mode. True = AUTO_ACCEPT (apply scorer's rating
        immediately). False = REQUIRE_APPROVAL (stage in SCORED_PENDING_APPROVAL until the
        user approves or rejects)."""
        return self._auto_approve_auto_score

    @auto_approve_auto_score.setter
    def auto_approve_auto_score(self, value: bool) -> None:
        if self._auto_approve_auto_score == value:
            return
        self._auto_approve_auto_score = value
        self._emit(self.dirty)

    def toggle_auto_approve_auto_score(self) -> None:
        self.auto_approve_auto_score = not self._auto_approve_auto_score

    def accept_all_auto_scores(self) -> None:
        """Approve every card currently in SCORED_PENDING_APPROVAL with its staged
        rating. No-op if there are no such cards.
        """
        assert self.state == FlashcardReviewViewModel.State.REVIEWING

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
                self._emplace_back_fixing_current(card)
                self._remaining_before_batched_autoscore.add(card.id)

        self._goto_next_unscored_card()
        self._emit(self.dirty)
        self._check_ready_to_autoscore()
        self._check_done()


    @property
    def autoscore_in_progress(self) -> bool:
        return self._autoscore_task is not None and not self._autoscore_task.done()

    @property
    def num_remaining(self) -> int:
        """Number of cards still in the current round's remaining set."""
        return len(self._remaining_before_batched_autoscore)

    @property
    def remaining_position(self) -> int | None:
        """1-indexed position of the current card among the remaining cards (ordered by their index in
        ``_cards``). ``None`` if the current card isn't in the remaining set (e.g. already scored, or in
        AWAITING_REVEAL)."""
        current = self.current_card
        if current is None:
            return None
        if current.id not in self._remaining_before_batched_autoscore:
            return None
        pos = 0
        for card in self._cards:
            if card.id in self._remaining_before_batched_autoscore:
                pos += 1
                if card is current:
                    return pos
        return None

    def begin(self):
        """Transition state from START to REVIEWING."""
        assert self.state == FlashcardReviewViewModel.State.START

        self.state = FlashcardReviewViewModel.State.REVIEWING
        # Kick off the first card's think-time timer.
        self._unpause_current_if_front()

        self._emit(self.dirty)

    def cancel(self):
        """Transition to the cancelled DONE state."""
        assert self.state != FlashcardReviewViewModel.State.DONE
        self._cancelled = True
        self.finish()

    def finish(self):
        """Transition to the DONE state."""
        assert self.state != FlashcardReviewViewModel.State.DONE

        # Pause the current card's timer if it's still running (the session is ending mid-think, e.g. on
        # ctrl+c). Must be done before state transition since Flashcard.pause() asserts state == FRONT.
        self._pause_current_if_front()

        self.state = FlashcardReviewViewModel.State.DONE

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

        self._emit(self.dirty)

    def toggle_collapsed(self):
        assert self.state == FlashcardReviewViewModel.State.DONE
        self._collapsed = not self._collapsed
        self._emit(self.dirty)

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

    def next_card(self):
        """Navigate to the next card, wrapping around if necessary. Does not change card state (other than pausing/unpausing the think-time timer)."""
        assert self.state != FlashcardReviewViewModel.State.START
        if not self._cards:
            return

        self._pause_current_if_front()
        self._step_card(1)
        self._unpause_current_if_front()
        self._emit(self.dirty)


    def prev_card(self):
        """Navigate to the previous card, wrapping around if necessary. Does not change card state (other than pausing/unpausing the think-time timer)."""
        assert self.state != FlashcardReviewViewModel.State.START
        if not self._cards:
            return

        self._pause_current_if_front()
        self._step_card(-1)
        self._unpause_current_if_front()
        self._emit(self.dirty)

    def score_current_card(self, score: Flashcard.Score):
        """Score the current card with the given score, transitioning card state accordingly."""
        assert self.state == FlashcardReviewViewModel.State.REVIEWING
        
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
            self._goto_next_unscored_card()

        elif score == Flashcard.Score.SKIPPED:
            self.current_card.skip()
            self._goto_next_unscored_card()

        # EASY/GOOD/HARD/AGAIN: apply the rating, then branch on the post-rating FSRS state.
        #   - State.Review
        #       - graduated; card lands in SCORED; already removed from both queues above.
        #   - State.Learning/Relearning
        #       - still in the (re)learning step ladder card lands in AWAITING_REVEAL and
        #         must come back this session. Requeue to the back of _cards and add to
        #        _next_remaining (swapped in once the current round drains).
        elif score in [
            Flashcard.Score.EASY,
            Flashcard.Score.GOOD,
            Flashcard.Score.HARD,
            Flashcard.Score.AGAIN,
        ]:
            current_card = self.current_card
            current_card.set_score(score)

            if current_card.fsrs_card.state in (State.Learning, State.Relearning):
                # Emplace this card at the back of the _cards list. Note that this implicitly moves the
                # cursor: for middle/first positions it shifts a new card into _current_card_index; for
                # the last position it leaves the cursor on the just-requeued card.
                self._cards.remove(current_card)
                self._cards.append(current_card)
                self._next_remaining_before_batched_autoscore.add(current_card.id)

            # Land on the next unscored card. If the implicit shift above already placed us on an unscored
            # card, _goto will stay put and just start that card's timer.
            self._goto_next_unscored_card()

        self._emit(self.dirty)

        # Check if we need to trigger a batched auto-score, or transition to DONE after scoring this card.
        # Order matters: if a batch is dispatched, _check_done correctly stays its hand because the pending
        # cards aren't terminally scored yet.
        self._check_ready_to_autoscore()
        self._check_done()


    # ------------------------------------------------------------------
    # Private Helpers
    # ------------------------------------------------------------------

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

    def _emplace_back_fixing_current(self, card):
        pos = self._cards.index(card)
        self._cards.pop(pos)
        if pos < self._current_card_index:
            self._current_card_index -= 1
        self._cards.append(card)

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

    def _check_ready_to_autoscore(self):
        """If the current round has drained and there are cards waiting on auto-scoring, swap in the next
        round and dispatch a batch.

        Round rollover is intentionally bundled here: by the time ``_remaining`` drains, every card the
        user has ratings-of-record for in this round either landed in SCORED (terminal) or in
        REVEALED_PENDING_AUTO_SCORE (waiting for the batch). The ``_next_remaining`` carry-over is the
        right input for the next round.
        """
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
        self._emit(self.dirty)

    def _check_done(self):
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
            self.finish()


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

        # Requeued cards (auto-accept mode, post-rating FSRS state in Learning/Relearning) get moved to
        # the back of _cards.
        for card in requeued:
            self._emplace_back_fixing_current(card)
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

        self._emit(self.dirty)

        # Pick up anything the user drained while the batch was running (may dispatch a follow-up batch),
        # then close the session if every card is now terminally scored.
        self._check_ready_to_autoscore()
        self._check_done()


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
