import asyncio
import time
from collections.abc import Callable
from datetime import datetime
from enum import Enum, auto
from typing import Any, NotRequired, TypedDict

from fsrs import Rating
from textual import events

from rhizome.db.operations.flashcards import apply_rating
from rhizome.logs import get_logger

_logger = get_logger("tui.flashcard_review_vm")


class Timer:
    """A simple start/pause/stop stopwatch.

    All three mutating calls are lenient — calling them in a state where
    they don't apply is a no-op rather than an error, so callers don't have
    to track state themselves. The one exception is ``start()`` after
    ``stop()``: ``stop`` is terminal (you've committed to a final elapsed
    value), so restarting requires an explicit ``reset()``.
    """

    def __init__(self):
        self._started = False
        self._paused = False
        self._stopped = False
        self._start_time: float | None = None
        self._accumulated: float = 0.0  # elapsed time from previous run segments

    @property
    def started(self) -> bool:
        return self._started

    @property
    def running(self) -> bool:
        """True iff the timer is actively ticking — started, not paused, not stopped."""
        return self._started and not self._paused and not self._stopped

    def start(self):
        """Start the timer or resume it from paused. Idempotent if running.
        Raises if the timer was previously ``stop()``-ed (stop is terminal)."""
        if self._stopped:
            raise RuntimeError("Cannot start a stopped timer. Call reset() first.")
        if self.running:
            return
        self._started = True
        self._paused = False
        self._start_time = time.perf_counter()

    def pause(self):
        """Pause the timer. No-op if not running."""
        if not self.running:
            return
        self._accumulated += time.perf_counter() - self._start_time
        self._start_time = None
        self._paused = True

    def stop(self) -> float:
        """Finalize the timer and return the total elapsed time. No-op
        (returns the accumulated total) if already stopped."""
        if self._stopped:
            return self._accumulated
        if self.running:
            self._accumulated += time.perf_counter() - self._start_time
        self._start_time = None
        self._stopped = True
        return self._accumulated

    def reset(self):
        self._started = False
        self._paused = False
        self._stopped = False
        self._start_time = None
        self._accumulated = 0.0

    def elapsed(self) -> float:
        if not self._started:
            return 0.0

        if self._paused or self._stopped:
            return self._accumulated

        return self._accumulated + (time.perf_counter() - self._start_time)

# Simple dataclass constructed in the interrupt payload in the tool call
class FlashcardData(TypedDict):
    question: str
    answer: str
    id: int
    testing_notes: NotRequired[str]

class Flashcard:

    class State(Enum):
        FRONT = auto()
        REVEALED_NOT_SCORED = auto()
        REVEALED_PENDING_AUTO_SCORE = auto()
        SCORED = auto()
        AWAITING_REVEAL = auto()

    class Score(Enum):
        # Remark: these four values intentionally mirror fsrs.Rating
        AGAIN = 1
        HARD = 2
        GOOD = 3
        EASY = 4
        # These values don't map on to FSRS, so we don't care
        AUTO = auto()
        SKIPPED = auto()

    def __init__(
        self,
        flashcard_data: FlashcardData,
        session_factory: Any
    ):
        self.question = flashcard_data["question"]
        self.answer = flashcard_data["answer"]
        self.id = flashcard_data["id"]
        self.testing_notes = flashcard_data.get("testing_notes", None)
        self._session_factory = session_factory

        # Timer for while the card is visible
        self._timer = Timer()
        # Timer for when the card is due (after being marked "again")
        self._due_timer: Timer | None = None
        self._due: float | None = None

        # Always start in the FRONT state
        self.state = Flashcard.State.FRONT

        # State variables
        self._score: Flashcard.Score | None = None
        self._timer_visible: bool = False # By default not visible
        self._user_answer: str | None = None
        self._awaiting_reveal_task: asyncio.Task | None = None
        # Set to True if the auto-scorer has previously failed on this card
        # (either returned an invalid result or crashed the whole batch).
        # Once set, the card cannot be re-deferred to the auto-scorer —
        # ``set_score_auto`` will assert and the FlashcardReview enter-path
        # falls back to a manual GOOD rating. Cleared by ``reset()``.
        self._auto_scoring_failed: bool = False

        # Fired from ``_wait_until_due`` after the card auto-reveals from
        # AWAITING_REVEAL back to FRONT. The VM wires this up to its
        # ``dirty`` emit so listeners get notified of the async transition
        # (which otherwise bypasses all VM methods).
        self._on_due_reveal: Callable[[], None] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def score(self) -> Score | None:
        return self._score
    
    @property
    def scored(self) -> bool:
        return self._score != None
    
    @property
    def elapsed_time(self) -> float:
        return self._timer.elapsed()
    
    @property
    def timer_visible(self) -> bool:
        return self._timer_visible
    
    @property
    def user_answer(self) -> str | None:
        return self._user_answer

    @property
    def auto_scoring_failed(self) -> bool:
        return self._auto_scoring_failed
    
    def set_user_answer(self, answer: str):
        assert self.state == Flashcard.State.FRONT
        self._user_answer = answer

    def unpause(self):
        """Resume the think-time timer. Only valid in FRONT (the only state
        where the timer should ever be running). Idempotent w.r.t. the
        underlying Timer."""
        assert self.state == Flashcard.State.FRONT
        self._timer.start()

    def pause(self):
        """Pause the think-time timer. Only valid in FRONT. Idempotent."""
        assert self.state == Flashcard.State.FRONT
        self._timer.pause()

    def toggle_timer_visible(self):
        self._timer_visible = not self._timer_visible

    def reset(self):
        self._timer.reset()
        self._score = None
        # Reset is a fresh start — the user explicitly opted to retry this
        # card, so they get a clean crack at auto-scoring too, and the
        # previously-typed draft answer is cleared so the input shows empty
        # when the card comes back around.
        self._auto_scoring_failed = False
        self._user_answer = None

        if self._awaiting_reveal_task is not None:
            self._awaiting_reveal_task.cancel()
            self._awaiting_reveal_task = None

        self.state = Flashcard.State.FRONT

    def reveal_back(self):
        assert self.state == Flashcard.State.FRONT
        # Stop timing "think time" — user has committed to revealing.
        self.pause()
        self.state = Flashcard.State.REVEALED_NOT_SCORED

    def reveal_front(self):
        assert self.state == Flashcard.State.AWAITING_REVEAL

        # Reset due timer and awaiting reveal task
        self._due = None

        if self._due_timer and self._due_timer.started:
            self._due_timer.stop()

        if self._awaiting_reveal_task is not None:
            self._awaiting_reveal_task.cancel()
            self._awaiting_reveal_task = None

        # Back to FRONT — transition state first, then resume the timer
        # (self.unpause requires state == FRONT).
        self.state = Flashcard.State.FRONT
        self.unpause()

    async def set_score(self, score: Score):
        """Score the card with the given score, transitioning state accordingly."""

        # First, delegate to other methods whenever possible
        if score == Flashcard.Score.SKIPPED:
            self.skip()
            return
        elif score == Flashcard.Score.AGAIN:
            await self.again()
            return
        elif score == Flashcard.Score.AUTO:
            self.set_score_auto()
            return
        
        # Can only set the score to EASY/GOOD/HARD if we're in one of the following two states
        assert self.state in [
            # This state reflects the user manually scoring the card
            Flashcard.State.REVEALED_NOT_SCORED,
            # This state reflects the auto-scorer scoring the card, or the user manually scoring a card that was pending auto-score
            Flashcard.State.REVEALED_PENDING_AUTO_SCORE
        ]
        
        self._score = score
        self._timer.stop()

        if score in [
            Flashcard.Score.EASY,
            Flashcard.Score.GOOD,
            Flashcard.Score.HARD,
        ]:
            async with self._session_factory() as session:
                await apply_rating(session, self.id, Rating(score.value))

        self.state = Flashcard.State.SCORED

    def set_score_auto(self):
        """Non-async version of set_score for the AUTO case."""
        assert self.state == Flashcard.State.REVEALED_NOT_SCORED
        assert not self._auto_scoring_failed, (
            "Cannot defer to auto-scoring: this card's previous auto-score "
            "failed. Call reset() first if the user explicitly wants to retry."
        )

        self._score = Flashcard.Score.AUTO
        self._timer.stop()
        self.state = Flashcard.State.REVEALED_PENDING_AUTO_SCORE

    def _revert_auto_score_failure(self):
        """Friend-method used by ``FlashcardReview._auto_score`` when the
        scorer fails to produce a valid rating for this card.

        Rolls the card back from ``REVEALED_PENDING_AUTO_SCORE`` to
        ``REVEALED_NOT_SCORED`` and clears the AUTO score, while latching
        ``auto_scoring_failed`` so the user can't re-defer this card (they
        must rate it manually, or ``reset()`` it to try again).
        """
        assert self.state == Flashcard.State.REVEALED_PENDING_AUTO_SCORE
        self._score = None
        self._auto_scoring_failed = True
        self.state = Flashcard.State.REVEALED_NOT_SCORED

    def skip(self):
        """Mark the card as skipped, which is effectively a score of SKIPPED and transition to SCORED state."""
        assert self.state in [
            Flashcard.State.FRONT,
            Flashcard.State.REVEALED_NOT_SCORED,
            Flashcard.State.AWAITING_REVEAL,
        ]

        self._score = Flashcard.Score.SKIPPED
        self._timer.stop()

        if self.state == Flashcard.State.AWAITING_REVEAL:
            # If the card was still awaiting reveal, we need to stop the due timer and awaiting reveal task
            self._due = None

            if self._due_timer and self._due_timer.started:
                self._due_timer.stop()

            if self._awaiting_reveal_task is not None:
                self._awaiting_reveal_task.cancel()
                self._awaiting_reveal_task = None

        self.state = Flashcard.State.SCORED

    async def again(self):
        assert self.state in [
            Flashcard.State.REVEALED_NOT_SCORED,
            Flashcard.State.REVEALED_PENDING_AUTO_SCORE
        ]

        self.reset()
        self.state = Flashcard.State.AWAITING_REVEAL

        async with self._session_factory() as session:
            updated_fc = await apply_rating(session, self.id, Rating.Again)
            due = (updated_fc.due - datetime.now()).total_seconds()

        self._due_timer = Timer()
        self._due = due

        self._due_timer.start()
        self._awaiting_reveal_task = asyncio.create_task(self._wait_until_due(due))

    @property
    def due_in(self) -> float | None:
        if self.state != Flashcard.State.AWAITING_REVEAL:
            return None
        
        return self._due - self._due_timer.elapsed()

    async def _wait_until_due(self, due: float):
        await asyncio.sleep(due)

        # Graceful handling in case the card was reset or scored while we were waiting
        if self.state == Flashcard.State.AWAITING_REVEAL:
            self.reveal_front()
            if self._on_due_reveal is not None:
                self._on_due_reveal()



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
        auto_scorer: Any = None
    ):
        super().__init__()
        self._session_factory = session_factory
        self._cards = [Flashcard(card, session_factory) for card in cards]
        self._current_card_index = 0

        self._auto_score_enabled = auto_score_enabled
        self._auto_scorer = auto_scorer

        # Internal state
        self.state = FlashcardReviewViewModel.State.START
        self._cancelled = False
        self._collapsed = False
        self._autoscore_task: asyncio.Task | None = None

        self._remaining_before_batched_autoscore = set(card.id for card in self._cards)
        self._next_remaining_before_batched_autoscore = set()

        # Single "something changed" observer list. Views subscribe a render
        # method that reads the whole VM each time. Fired once per public
        # transition method.
        self.dirty: list[Callable[[], None]] = []

        # Bridge the async due-timer reveal (which happens inside Flashcard,
        # not a VM method) into the dirty emit.
        for card in self._cards:
            card._on_due_reveal = lambda: self._emit(self.dirty)

    def _emit(self, listeners: list[Callable[[], None]]) -> None:
        for listener in listeners:
            listener()

    
    async def on_key(self, event: events.Key) -> None:
        # Valid actions per state:
        #
        # START
        #   - enter -> begin
        #   - ctrl+c -> cancel
        #
        # REVIEWING
        #   - enter     -> reveal or rate
        #   - 1/2/3/4   -> rate 1/2/3/4
        #   - alt+left  -> prev card
        #   - alt+right -> next card
        #   - ctrl+c    -> cancel session
        #   - ctrl+k    -> toggle timer display
        #   new ones:
        #   - alt+x     -> reset current card
        #   - alt+s     -> toggle skipped
        #
        # DONE
        #   - enter     -> collapse/expanded
        #   - alt+left (expanded) -> prev card
        #   - alt+right (expanded) -> next card

        _logger.info("on_key: state=%s key=%r", self.state.name, event.key)
        match self.state:
            case FlashcardReviewViewModel.State.START:
                await self._on_key_start(event)
            case FlashcardReviewViewModel.State.REVIEWING:
                await self._on_key_reviewing(event)
            case FlashcardReviewViewModel.State.DONE:
                await self._on_key_done(event)


    async def _on_key_start(self, event: events.Key) -> None:
        # START
        #   - enter -> begin
        #   - ctrl+c -> cancel
        assert self.state == FlashcardReviewViewModel.State.START

        # Remark: do we want to be .stop()ing here?
        if event.key == "enter":
            self.begin()
        elif event.key == "ctrl+c":
            self.cancel()
            #event.stop()

    async def _on_key_reviewing(self, event: events.Key) -> None:
        # REVIEWING
        #   - enter     -> reveal or rate
        #   - 1/2/3/4   -> rate 1/2/3/4
        #   - alt+left  -> prev card
        #   - alt+right -> next card
        #   - ctrl+c    -> cancel session
        #   - ctrl+k    -> toggle timer display
        #   new ones:
        #   - alt+x     -> reset current card
        #   - alt+s     -> toggle skipped
        assert self.state == FlashcardReviewViewModel.State.REVIEWING

        if event.key == "alt+left":
            self.prev_card()

        elif event.key == "alt+right":
            self.next_card()

        elif event.key == "ctrl+c":
            self.cancel()
            #event.stop()

        elif event.key == "ctrl+k":
            if self.current_card:
                self.current_card.toggle_timer_visible()
                self._emit(self.dirty)

        elif event.key == "alt+x":
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
                # If the card was previously scored, we need to add it back to the remaining queue. If it was already in the remaining queue, no-op.
                self._next_remaining_before_batched_autoscore.discard(self.current_card.id)
                self._remaining_before_batched_autoscore.add(self.current_card.id)
                self._emit(self.dirty)

        elif event.key == "alt+s":
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


        elif event.key in ["1", "2", "3", "4"]:
            if self.current_card and self.current_card.state in [
                Flashcard.State.REVEALED_NOT_SCORED,
                Flashcard.State.REVEALED_PENDING_AUTO_SCORE
            ]:
                score_mapping = {
                    "1": Flashcard.Score.AGAIN,
                    "2": Flashcard.Score.HARD,
                    "3": Flashcard.Score.GOOD,
                    "4": Flashcard.Score.EASY,
                }
                await self.score_current_card(score_mapping[event.key])

        elif event.key == "enter":
            if self.current_card:
                if self.current_card.state == Flashcard.State.FRONT:
                    self.current_card.reveal_back()
                    self._emit(self.dirty)
                elif self.current_card.state == Flashcard.State.AWAITING_REVEAL:
                    self.current_card.reveal_front()
                    self._emit(self.dirty)

                elif self.current_card.state == Flashcard.State.REVEALED_NOT_SCORED:
                    # Default enter-on-revealed action depends on config and
                    # on whether this card has already failed auto-scoring.
                    if (
                        self._auto_score_enabled
                        and not self.current_card.auto_scoring_failed
                    ):
                        await self.score_current_card(Flashcard.Score.AUTO)
                    else:
                        # Either auto-scoring is disabled, or this card has
                        # already burned its auto-score attempt — user needs
                        # to rate manually. Default to GOOD.
                        await self.score_current_card(Flashcard.Score.GOOD)

            
    async def _on_key_done(self, event: events.Key) -> None:
        # DONE
        #   - enter     -> collapse/expanded
        #   - alt+left (expanded) -> prev card
        #   - alt+right (expanded) -> next card
        assert self.state == FlashcardReviewViewModel.State.DONE

        if event.key == "alt+left":
            self.prev_card()

        elif event.key == "alt+right":
            self.next_card()

        elif event.key == "enter":
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
    def autoscore_in_progress(self) -> bool:
        return self._autoscore_task is not None and not self._autoscore_task.done()

    @property
    def num_remaining(self) -> int:
        """Number of cards still in the current round's remaining set."""
        return len(self._remaining_before_batched_autoscore)

    @property
    def remaining_position(self) -> int | None:
        """1-indexed position of the current card among the remaining
        cards (ordered by their index in ``_cards``). ``None`` if the
        current card isn't in the remaining set (e.g. already scored, or
        in AWAITING_REVEAL)."""
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

        # Pause the current card's timer if it's still running (the session
        # is ending mid-think, e.g. on ctrl+c). Must be done before state
        # transition since Flashcard.pause() asserts state == FRONT.
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

    async def score_current_card(self, score: Flashcard.Score):
        """Score the current card with the given score, transitioning card state accordingly."""
        assert self.state == FlashcardReviewViewModel.State.REVIEWING
        
        if self.current_card is None:
            return
        
        assert self.current_card.state in [
            Flashcard.State.REVEALED_NOT_SCORED,
            Flashcard.State.REVEALED_PENDING_AUTO_SCORE
        ]

        # We can reach this method for a card in two states:
        #   1) REVEALED_NOT_SCORED: this happens when the user manually scores a card that they just revealed
        #   2) REVEALED_PENDING_AUTO_SCORE: this happens when either the user manually scores a card that was pending auto-score
        #
        # In this latter case, we additionally need to guard against an in-flight autoscore task (the user score would be immediately
        # overridden by the auto-score once complete).

        if self.current_card.state == Flashcard.State.REVEALED_PENDING_AUTO_SCORE:
            if self.autoscore_in_progress:
                return

        # If the card was rated EASY/GOOD/HARD, we just need to update its state to SCORED.
        # AUTO is handled the same as EASY/GOOD/HARD, until we need to perform a batched autoscore.

        self._remaining_before_batched_autoscore.discard(self.current_card.id)

        if score in [
            Flashcard.Score.EASY,
            Flashcard.Score.GOOD,
            Flashcard.Score.HARD,
        ]:
            await self.current_card.set_score(score)
            self._goto_next_unscored_card()
        
        elif score == Flashcard.Score.AUTO:
            # Routed separately to utilize specialized non-async method
            self.current_card.set_score_auto()
            self._goto_next_unscored_card()

        elif score == Flashcard.Score.SKIPPED:
            # Similar: routed separately to utilize specialized non-async method
            self.current_card.skip()
            self._goto_next_unscored_card()

        # If the card was scored AGAIN, we need to requeue it by moving it to the end of the list and resetting its state.
        #   - Additionally, we need to add it to the next round of "remaining_before_batched_autoscore", which gets swapped
        #     in once the first round is entirely drained of remaining cards.
        elif score == Flashcard.Score.AGAIN:
            # Emplace this card at the back of the _cards list. Note that
            # this implicitly moves the cursor: for middle/first positions
            # it shifts a new card into _current_card_index; for the last
            # position it leaves the cursor on the just-AGAIN'd card.
            current_card = self.current_card
            self._cards.remove(current_card)
            self._cards.append(current_card)

            # And, add to next round of remaining:
            self._next_remaining_before_batched_autoscore.add(current_card.id)

            # Set the card internally to the "again" state — this also
            # resets its main timer (AWAITING_REVEAL isn't timed).
            await current_card.again()

            # Land on the next unscored card. If the implicit shift above
            # already placed us on an unscored card, _goto will stay put
            # and just start that card's timer.
            self._goto_next_unscored_card()

        self._emit(self.dirty)

        # Check if we need to transition to DONE state after scoring this card, or if we need to trigger a batched auto-score
        self._check_remaining_cards()


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

    def _goto_next_unscored_card(self):
        """Land the cursor on the next card still needing attention.

        Prefers cards in the current round's ``_remaining`` set; falls back
        to ``_next_remaining`` if the current round is drained. The
        fallback matters when e.g. the first card was AGAIN'd and the rest
        of the round was completed — without it, the cursor would stall on
        the just-scored last card instead of landing on the AGAIN'd card
        (which is now in AWAITING_REVEAL, waiting for its due timer).

        - If the current card is already in the preferred set, stays put.
        - Otherwise, walks forward cyclically until a matching card is
          found. If the first pass (``_remaining``) yields nothing, tries
          ``_next_remaining`` before giving up.

        Manages the think-time timer across the move: the outgoing card is
        paused (if in FRONT) and the landing card is unpaused (if in FRONT).
        The unpause fires even when we stay put, because callers may invoke
        this after an implicit cursor shift (e.g. the AGAIN ``remove +
        append`` that promotes a new card into the cursor's position) where
        the landing card's timer hasn't been started yet.
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
                # Stay put, but make sure the timer is running for the
                # landing card (it may have been implicitly shifted into
                # place via the AGAIN remove+append).
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
                    # Full loop, nothing in this set — fall through to
                    # the next target_set (or exit entirely).
                    break

        # Both sets empty or exhausted — leave cursor where it is and
        # make sure timer state is consistent.
        self._unpause_current_if_front()

    def _check_remaining_cards(self):

        # Check if we've drained the remaining cards
        if not self._remaining_before_batched_autoscore:

            # Guard against re-entry while a batch is already running. The
            # running batch's completion handler will call back into this
            # method once it's done.
            if self.autoscore_in_progress:
                return

            # Now that we've scored all the _first_ round of cards, we swap in the _next_ round of remaining cards,
            # and repeat the process for the new round of cards (the user can mark these as "again" still).
            self._remaining_before_batched_autoscore = self._next_remaining_before_batched_autoscore
            self._next_remaining_before_batched_autoscore = set()

            # Check if we need to autoscore any cards
            pending_auto_score = [c for c in self._cards if c.score == Flashcard.Score.AUTO]

            # If nothing to autoscore, check if we're done.
            if not pending_auto_score:
                if not self._remaining_before_batched_autoscore:
                    self.finish()
                return

            # Otherwise, spawn a task to handle the batched auto-score
            self._autoscore_task = asyncio.create_task(self._handle_batched_auto_score(pending_auto_score))
            self._emit(self.dirty)


    async def _handle_batched_auto_score(self, pending_auto_score: list[Flashcard]) -> None:
        try:
            again, failed = await self._auto_score(pending_auto_score)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _logger.warning("Batch auto-scoring failed: %s", exc)
            # Whole-batch failure: revert every card that's still sitting in
            # REVEALED_PENDING_AUTO_SCORE (i.e. the scorer didn't get to
            # produce a rating for it before blowing up). Anything that
            # already made it through ``set_score`` is already committed.
            again = []
            failed = []
            for card in pending_auto_score:
                if card.state == Flashcard.State.REVEALED_PENDING_AUTO_SCORE:
                    card._revert_auto_score_failure()
                    failed.append(card)

        # Re-queue each AGAIN card at the back of ``_cards`` while keeping
        # ``_current_card_index`` pointing at the same underlying card.
        for card in again:
            pos = self._cards.index(card)
            self._cards.pop(pos)
            if pos < self._current_card_index:
                self._current_card_index -= 1
            self._cards.append(card)
            # The card is already in AWAITING_REVEAL (from Flashcard.again()
            # via set_score). Just add it to this round's remaining bucket.
            self._remaining_before_batched_autoscore.add(card.id)

        # Failed cards stay in place but go back into the remaining set so
        # the user can pick them up and rate them manually.
        for card in failed:
            self._remaining_before_batched_autoscore.add(card.id)

        # The cursor may be sitting on a card that's now SCORED; advance it
        # to the next unscored card (if any).
        self._goto_next_unscored_card()

        # Clear the task handle _before_ any follow-up calls that might
        # observe ``autoscore_in_progress`` or try to cancel us.
        self._autoscore_task = None

        self._emit(self.dirty)

        # Pick up anything the user drained while the batch was running, and
        # either close the session or spin up the next batch if needed.
        self._check_remaining_cards()

        # If we're still sitting on an empty round after the re-check, the
        # session is complete.
        if (
            self.state == FlashcardReviewViewModel.State.REVIEWING
            and not self._remaining_before_batched_autoscore
            and not any(c.score == Flashcard.Score.AUTO for c in self._cards)
        ):
            self.finish()


    async def _auto_score(
        self, pending_auto_score: list[Flashcard]
    ) -> tuple[list[Flashcard], list[Flashcard]]:
        """Batch-score every pending-auto card via the scorer subagent.

        For each card, applies the rating returned by the scorer via
        ``Flashcard.set_score`` — which routes AGAIN through ``again()`` and
        EASY/GOOD/HARD through ``apply_rating``.

        Returns a tuple ``(again_cards, failed_cards)``:

        - ``again_cards``: cards the scorer rated AGAIN. Caller must
          requeue them at the back of the display order.
        - ``failed_cards``: cards the scorer couldn't score (dropped from
          the response, non-integer, or out-of-range). These have been
          reverted via ``Flashcard._revert_auto_score_failure`` — they're
          now ``REVEALED_NOT_SCORED`` with ``auto_scoring_failed == True``
          and need to go back into ``_remaining`` for manual rating.

        Uses the stable flashcard id as ``flashcard_id`` in the prompt so
        results map back unambiguously, regardless of return order.
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

        again_cards: list[Flashcard] = []
        failed_cards: list[Flashcard] = []

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

            score = score_map[rating]
            await card.set_score(score)
            if score == Flashcard.Score.AGAIN:
                again_cards.append(card)

        return again_cards, failed_cards
