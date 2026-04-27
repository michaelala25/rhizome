"""View-model for the FlashcardReview widget.

This module owns two interacting state machines: ``Flashcard`` (per-card lifecycle) and
``FlashcardReviewViewModel`` (session-level orchestration over a list of cards). The view
(``FlashcardReview``) subscribes to a single ``dirty`` observer list on the VM and re-renders
on every emit; it never mutates VM state directly.

============================================================================================
Flashcard state machine
============================================================================================

States:
    FRONT
        Initial state. The question is shown; the user is thinking. The think-time ``Timer``
        is the only timer that runs in this state, and it runs ONLY in this state.
    REVEALED_NOT_SCORED
        The back of the card has been revealed; the user has not yet rated it.
    REVEALED_PENDING_AUTO_SCORE
        The user deferred this card to the auto-scorer (Score.AUTO).
    SCORED
        Terminal scored state. Score is one of EASY/GOOD/HARD/SKIPPED. Reached when a
        rating advances ``_current_fsrs_card`` to ``State.Review`` (the card has graduated
        from the (re)learning step ladder), or directly via ``skip()``. AGAIN never lands
        here in practice — Rating.Again always produces Learning or Relearning.
    AWAITING_REVEAL
        A rating has been applied and ``_current_fsrs_card`` is in ``State.Learning`` or
        ``State.Relearning`` — the card needs to come back this session before the user
        is done with it. The card is scheduled and waiting for its due delta to elapse
        before being shown again. A ``_wait_until_due`` task is in flight; ``_due_timer``
        ticks the countdown. Reached on AGAIN (always — Again never graduates) and on
        HARD/GOOD/EASY when the rating doesn't advance the card out of the step ladder.

Score values:
    AGAIN / HARD / GOOD / EASY      mirror ``fsrs.Rating`` (values 1-4).
    AUTO                            sentinel: card is deferred to the batch scorer.
    SKIPPED                         sentinel: card was passed over without rating.

Transitions (all enforced by asserts on the source state):

    FRONT
        -> REVEALED_NOT_SCORED [via reveal_back()]
            - The user issued the reveal action to flip the card and see the answer.
            - Pauses the think-time timer.

        -> SCORED(SKIPPED) [via skip()]
            - The user issued the skip action without first revealing the card.
            - Stops the think-time timer.


    REVEALED_NOT_SCORED
        -> SCORED [via set_score(HARD|GOOD|EASY); assuming next FSRS state is Review]
            - The user rated the card HARD/GOOD/EASY, and the FSRS scheduler determines the next 
              state of the card. If the new state is Learning or Relearning, then the card ends
              up in the AWAITING_REVEAL state, as it needs to be reviewed again before the session
              wraps up. Otherwise, if it enters the Review stage, it's transitioned to SCORED as a
              pseudo-terminal state.
        
        -> AWAITING_REVEAL [via set_score(AGAIN|HARD|GOOD); assuming next FSRS state is (Re)Learning]
            - Same as above, user has rated the card AGAIN/HARD/GOOD, and the FSRS scheduler determines
              that the next card state is either Learning or Relearning, in which case we need to
              requeue the card.

        -> REVEALED_PENDING_AUTO_SCORE [via set_score_auto(); requires !auto_scoring_failed]
            - The user issued the default-rate action on a revealed card while auto-scoring
              is enabled, deferring the rating to the batch scorer (the view's
              enter-on-revealed default).
            - Stops the think-time timer. Card waits in this state until the round-drain
              triggers ``_check_ready_to_autoscore`` to dispatch the batch.

        -> SCORED(SKIPPED) [via skip()]
            - The user issued the skip action after revealing.
            - Stops the think-time timer.


    REVEALED_PENDING_AUTO_SCORE
        -> SCORED or AWAITING_REVEAL [via set_score(EASY|GOOD|HARD|AGAIN)]
            - Either the user manually scored the card, or the batch auto-scorer returned a
              rating in {1,2,3,4}.
            - Manual override of a PENDING_AUTO card is blocked by
              ``FlashcardReviewViewModel`` while the auto-score task is in flight (the
              user's score would be immediately overridden once the batch returns).
            - Same routing as the REVEALED_NOT_SCORED variant: scheduler advances
              ``_current_fsrs_card``, then SCORED if Review or AWAITING_REVEAL if
              Learning/Relearning.

        -> REVEALED_NOT_SCORED [via _revert_auto_score_failure()]
            - The batch scorer dropped this card, returned a non-integer / out-of-range
              score, or the whole batch raised.
            - Latches ``auto_scoring_failed=True``, which forbids future ``set_score_auto()``
              calls on this card until ``reset()`` is invoked.


    AWAITING_REVEAL
        -> FRONT [via reveal_front()]
            - The user issued the reveal action to surface the card manually (impatient
              pre-empt of the due timer).
            - Cancels the in-flight ``_wait_until_due`` task and stops ``_due_timer``. Then
              starts the think-time timer (which is the only timer running in FRONT).

        -> FRONT [via _wait_until_due firing once due elapses]
            - The async sleep elapsed naturally. ``_wait_until_due`` calls ``reveal_front()``
              and then fires the VM-wired ``_on_due_reveal`` callback so the VM can emit
              ``dirty`` (this transition otherwise bypasses every public VM method).

        -> SCORED(SKIPPED) [via skip()]
            - The view-model's ``finish()`` reaches every AWAITING_REVEAL card and skips it
              when the session ends (cancel or natural finish), since these cards will not be
              coming back around. The user-initiated skip action does NOT reach this branch:
              ``_on_key_reviewing`` returns early on AWAITING_REVEAL for skip purposes.
            - Cancels the in-flight ``_wait_until_due`` task and stops ``_due_timer``.


    {any state}
        -> FRONT [via reset()]
            - Triggered by the user-initiated reset action on the current card and by the
              unskip action on a SCORED(SKIPPED) card. (``_apply_rating`` does NOT call
              this; it uses ``_reset_session_metadata()`` directly when routing to
              AWAITING_REVEAL, so the rating it just applied isn't immediately rolled
              back.)
            - Clears ``_score``, ``_user_answer``, and ``auto_scoring_failed``; resets the
              think-time timer; cancels any in-flight ``_wait_until_due`` task; AND
              restores ``_current_fsrs_card`` to a copy of ``_initial_fsrs_card`` (this is
              the only operation that does so). Because no DB write has happened, this
              restoration is fully consistent.

              Remark: Resetting a card _after_ a DB commit is entirely valid, since each card
              maintains it's initial FSRS state, distinct from the DB. Future DB commits will
              simply overwrite the previous FSRS state with the newly reset state.

Per-card contracts:
    - The think-time ``Timer`` is started/paused only in FRONT. ``pause()`` and ``unpause()``
      both assert state == FRONT.
    - ``set_user_answer()`` asserts state == FRONT.
    - The scheduler is invoked from exactly two paths inside the ``Flashcard``:
      ``set_score(EASY/GOOD/HARD)`` and ``again()``. SKIPPED and AUTO do not advance FSRS
      state; AUTO defers to the batch (which routes each card through ``set_score`` once
      the scorer returns), and SKIPPED is a session-local outcome only.
    - ``auto_scoring_failed`` latches when a card's auto-score result is dropped, malformed,
      out of range, or the whole batch raised. While latched, ``set_score_auto`` is forbidden
      (asserts) and the view's default-rate-on-revealed path falls back to a manual GOOD rating.
      ``reset()`` clears the latch.

============================================================================================
FSRS state ownership
============================================================================================

The VM owns a single ``fsrs.Scheduler`` and is the only object that runs ratings through it.
Each ``Flashcard`` carries two in-memory ``fsrs.Card`` snapshots:

    _initial_fsrs_card
        Immutable snapshot of the card's FSRS state at session start. ``reset()`` rolls
        ``_current_fsrs_card`` back to a copy of this.
    _current_fsrs_card
        Mutated by ``set_score`` / ``again`` (each rating advances the in-memory card via
        the VM-owned scheduler). Never persisted automatically.

No DB I/O happens during the session. The VM exposes a single public ``commit()`` coroutine
that writes each card's ``_current_fsrs_card`` back to the DB; it is never called internally
and is reserved for the widget's caller (the ``review_present_flashcards`` tool) to invoke
when the session ends against a non-ephemeral DB ``ReviewSession``. ``commit()`` is
idempotent: calling it twice writes the same state twice.

The session factory passed to the VM is held only for ``commit()``. The Flashcards do not
hold or use it.

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
import copy
import time
from collections.abc import Callable
from datetime import UTC, datetime
from enum import Enum, auto
from typing import Any, NotRequired, TypedDict

from fsrs import Card, Rating, Scheduler, State
from textual import events

from rhizome.db.operations.flashcards import commit_fsrs_card
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

class Action(Enum):
    """Semantic keyboard actions handled by the view-model.

    Multiple actions can map to the same key (e.g. all four ``enter``
    flavors in REVIEWING — REVEAL_BACK / REVEAL_FRONT / SCORE_DEFAULT /
    ADVANCE_NEXT — share ``"enter"``); the dispatcher disambiguates by
    state. Keeping them as separate Action members lets a help dropdown
    show contextual labels per state.
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

    # DONE
    TOGGLE_COLLAPSED = auto()  # enter

    # Cross-state UI toggle
    TOGGLE_HELP = auto()       # alt+h
    TOGGLE_AUTO_SCORE = auto() # alt+a


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
    Action.TOGGLE_COLLAPSED: "enter",
    Action.TOGGLE_HELP: "alt+h",
    Action.TOGGLE_AUTO_SCORE: "alt+a",
}


# Map of digit-key → score for the REVIEWING digit dispatch. Defined as
# a module-level closure that resolves Flashcard.Score lazily, since
# Flashcard is declared below.
def _score_key_map() -> dict[str, "Flashcard.Score"]:
    return {
        KEYBINDINGS[Action.SCORE_AGAIN]: Flashcard.Score.AGAIN,
        KEYBINDINGS[Action.SCORE_HARD]: Flashcard.Score.HARD,
        KEYBINDINGS[Action.SCORE_GOOD]: Flashcard.Score.GOOD,
        KEYBINDINGS[Action.SCORE_EASY]: Flashcard.Score.EASY,
    }


# Simple dataclass constructed in the interrupt payload in the tool call.
# ``fsrs_card`` is the in-memory FSRS scheduling state for this card at the
# start of the session — built from the DB row by ``to_fsrs_card`` (or
# constructed directly for tests / sample data). The Flashcard takes a
# private copy so the caller's object isn't mutated.
class FlashcardData(TypedDict):
    question: str
    answer: str
    id: int
    fsrs_card: Card
    testing_notes: NotRequired[str]

class Flashcard:

    class State(Enum):
        FRONT = auto()
        REVEALED_NOT_SCORED = auto()
        REVEALED_PENDING_AUTO_SCORE = auto()
        # The auto-scorer has produced a rating, but the user still has
        # to approve it (or discard it and rate manually). Reached only
        # in REQUIRE_APPROVAL auto-score mode. The proposed rating
        # lives in ``_pending_score`` — FSRS state is intentionally
        # unchanged while we wait for approval, so ``discard_pending_score``
        # is just a field-clearing operation with no rollback.
        SCORED_PENDING_APPROVAL = auto()
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
        scheduler: Scheduler,
    ):
        self.question = flashcard_data["question"]
        self.answer = flashcard_data["answer"]
        self.id = flashcard_data["id"]
        self.testing_notes = flashcard_data.get("testing_notes", None)
        self._scheduler = scheduler

        # FSRS scheduling state — managed entirely in memory. ``_initial`` is
        # the snapshot at session start (used by ``reset()`` to fully roll
        # the card back); ``_current`` is mutated by every rating. Neither
        # is ever written to the DB during the session — the VM's
        # ``commit()`` is the only sanctioned write site, and it's called
        # by the widget's owner, not internally.
        self._initial_fsrs_card = copy.copy(flashcard_data["fsrs_card"])
        self._current_fsrs_card = copy.copy(self._initial_fsrs_card)

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
        # Set to True when the user discards a SCORED_PENDING_APPROVAL
        # auto-score. Has the same suppressing effect on subsequent
        # auto-score attempts within the same attempt as
        # ``_auto_scoring_failed`` (the enter-path falls back to a manual
        # rating), but kept as a separate flag so the view can distinguish
        # "scorer broke" from "user rejected" in its on-screen message.
        # Cleared by ``reset()`` and by ``_reset_session_metadata()``.
        self._auto_score_discarded: bool = False

        # The proposed rating from the auto-scorer that the user has not
        # yet approved or discarded. Set by ``set_pending_score`` while
        # in REQUIRE_APPROVAL mode; consumed by ``approve_pending_score``
        # (which then runs the rating through the normal scoring path);
        # cleared by ``discard_pending_score``. The FSRS state is NOT
        # advanced while the rating sits here — discard is just clearing
        # the field, no rollback needed.
        self._pending_score: Flashcard.Score | None = None

        # Fired from ``_wait_until_due`` after the card auto-reveals from
        # AWAITING_REVEAL back to FRONT. The VM wires this up to its
        # ``dirty`` emit so listeners get notified of the async transition
        # (which otherwise bypasses all VM methods).
        self._on_due_reveal: Callable[[], None] | None = None

        # Cached per-rating "seconds-until-due" preview, computed once at
        # the FRONT -> REVEALED_NOT_SCORED transition. Cached because
        # Scheduler.review_card is non-deterministic (fuzzing), so we
        # don't want the displayed intervals to fluctuate every render.
        # Cleared whenever the FSRS state changes (reset / requeue).
        self._cached_rating_previews: dict[Rating, float] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def score(self) -> Score | None:
        return self._score

    @property
    def pending_score(self) -> Score | None:
        """The auto-scorer's proposed rating, while the card sits in
        SCORED_PENDING_APPROVAL. ``None`` in any other state."""
        return self._pending_score


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

    @property
    def fsrs_card(self) -> Card:
        """The card's current in-memory FSRS scheduling state.

        Mutated by ``set_score`` / ``again`` (which run the rating through
        the VM-owned scheduler), and rolled back to the initial snapshot
        by ``reset()``. Never persisted unless the VM's caller invokes
        ``commit()`` on the view-model.
        """
        return self._current_fsrs_card
    
    def rating_previews(self) -> dict[Rating, float]:
        """Cached per-rating "seconds-until-due" preview against the
        card's current FSRS state.

        Populated by ``_compute_rating_previews`` at the FRONT ->
        REVEALED_NOT_SCORED transition (when the rating row first
        becomes visible). Cached because ``Scheduler.review_card`` is
        non-deterministic (fuzzing) — without caching, the displayed
        intervals would jitter on every render.
        """
        if self._cached_rating_previews is None:
            self._cached_rating_previews = self._compute_rating_previews()
        return self._cached_rating_previews

    def _compute_rating_previews(self) -> dict[Rating, float]:
        now = datetime.now(UTC)
        previews: dict[Rating, float] = {}
        for rating in (Rating.Again, Rating.Hard, Rating.Good, Rating.Easy):
            preview, _ = self._scheduler.review_card(
                self._current_fsrs_card, rating, now,
            )
            previews[rating] = (preview.due - now).total_seconds()
        return previews

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

    def _reset_session_metadata(self):
        """Clear per-attempt session metadata WITHOUT touching FSRS state.

        Used by both the public ``reset()`` and by ``again()`` (which
        funnels into AWAITING_REVEAL — rolling the FSRS state back there
        would discard the Rating.Again that's about to be applied).
        """
        self._timer.reset()
        self._score = None
        # The user gets a clean crack at auto-scoring on the next attempt,
        # and the previously-typed draft answer is cleared so the input
        # shows empty when the card comes back around.
        self._auto_scoring_failed = False
        self._auto_score_discarded = False
        self._user_answer = None
        # Per-rating preview cache is per-attempt — drop it so the next
        # reveal_back recomputes against whatever FSRS state is current
        # by then.
        self._cached_rating_previews = None

        if self._awaiting_reveal_task is not None:
            self._awaiting_reveal_task.cancel()
            self._awaiting_reveal_task = None

    def reset(self):
        """Fully reset the card: clear session metadata AND restore FSRS
        state to the session's initial snapshot.

        Called when the user explicitly opts to retry the card from
        scratch (alt+x, or unskip via alt+s on a SKIPPED card). Because
        FSRS state hasn't been committed mid-session, restoring
        ``_current_fsrs_card`` to ``_initial_fsrs_card`` is fully
        consistent — the next rating starts from a true initial state.
        """
        self._reset_session_metadata()
        self._current_fsrs_card = copy.copy(self._initial_fsrs_card)
        # If the card was sitting in SCORED_PENDING_APPROVAL, drop the
        # staged rating along with everything else.
        self._pending_score = None
        self.state = Flashcard.State.FRONT

    def reveal_back(self):
        assert self.state == Flashcard.State.FRONT
        # Stop timing "think time" — user has committed to revealing.
        self.pause()
        self.state = Flashcard.State.REVEALED_NOT_SCORED
        # Lock in the per-rating previews now that the rating row is
        # about to become visible. See _cached_rating_previews.
        self._cached_rating_previews = self._compute_rating_previews()

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

    def set_score(self, score: Score):
        """Score the card with the given score, transitioning state accordingly.

        Synchronous — FSRS state is mutated in memory. No DB I/O happens here.

        For EASY/GOOD/HARD/AGAIN, the resulting state depends on the post-rating FSRS state: ``State.Review``
        lands in SCORED; ``State.Learning`` / ``State.Relearning`` land in AWAITING_REVEAL with a 
        ``_wait_until_due`` task scheduled from the new ``due``.
        """

        # Always stop the timer upon receiving any score. Every score either transitions the card to SCORED,
        # PENDING_AUTO_SCORE, or AWAITING_REVEAL. In the first two cases, the "time spent thinking before answering"
        # is fixed at the point of scoring - in the last case, the timer needs to be reset regardless.
        self._timer.stop()

        # First, delegate to other methods whenever possible
        if score == Flashcard.Score.SKIPPED:
            self.skip()
            return
        elif score == Flashcard.Score.AGAIN:
            self.again()
            return
        elif score == Flashcard.Score.AUTO:
            self.set_score_auto()
            return

        # Can only set the score to EASY/GOOD/HARD if we're in one of these states
        assert self.state in [
            # User manually scoring the card.
            Flashcard.State.REVEALED_NOT_SCORED,
            # Auto-scorer scoring the card, or user manually scoring a
            # card that was pending auto-score.
            Flashcard.State.REVEALED_PENDING_AUTO_SCORE,
            # User approving a pending auto-score (via approve_pending_score).
            Flashcard.State.SCORED_PENDING_APPROVAL,
        ]

        self._apply_rating(Rating(score.value), score)

    def _apply_rating(self, rating: Rating, score: Score) -> None:
        """Run the rating through the scheduler and route to either SCORED
        or AWAITING_REVEAL based on the resulting FSRS state.

        - ``State.Review`` → terminal SCORED. Stops the think-time timer and stamps ``_score = score``.
        - ``State.Learning`` / ``State.Relearning`` → AWAITING_REVEAL. Clears per-attempt session metadata
          (the next reveal is a fresh attempt: blank user_answer, fresh auto-score eligibility), spawns
          ``_wait_until_due`` from the FSRS-computed ``due`` delta.

        ``_score`` is None inside AWAITING_REVEAL because the requeue is a not-yet-finalized attempt — the
          user will rate again when it comes back around.
        """
        review_dt = datetime.now(UTC)
        self._current_fsrs_card, _log = self._scheduler.review_card(
            self._current_fsrs_card, rating, review_dt,
        )

        if self._current_fsrs_card.state == State.Review:
            # Graduated — terminal scored state.
            self._score = score
            self.state = Flashcard.State.SCORED
            return

        # Still inside the (re)learning step ladder — requeue the card
        # via the due timer. ``_reset_session_metadata`` clears _score
        # (consistent with AWAITING_REVEAL), drops the user_answer draft,
        # clears the auto_scoring_failed latch, and resets the think-time
        # timer for the next FRONT cycle. It does NOT touch FSRS state.
        self._reset_session_metadata()
        self.state = Flashcard.State.AWAITING_REVEAL

        due = (self._current_fsrs_card.due - review_dt).total_seconds()
        self._due_timer = Timer()
        self._due = due
        self._due_timer.start()
        self._awaiting_reveal_task = asyncio.create_task(self._wait_until_due(due))

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

    def set_pending_score(self, score: Score):
        """Stage a rating from the auto-scorer for user approval.

        Used by the batch auto-scorer when the VM is in REQUIRE_APPROVAL
        mode. Records the proposed rating in ``_pending_score`` and
        transitions to SCORED_PENDING_APPROVAL. FSRS state is
        intentionally untouched — the rating is only applied if the
        user approves, so ``discard_pending_score`` is a pure
        field-clearing operation.

        All four FSRS ratings (AGAIN/HARD/GOOD/EASY) are valid — the
        user gets the same approval gate over the model's call regardless
        of which way it went. SKIPPED is a user-only action and not a
        valid auto-score outcome.
        """
        assert self.state == Flashcard.State.REVEALED_PENDING_AUTO_SCORE
        assert score in (
            Flashcard.Score.AGAIN,
            Flashcard.Score.HARD,
            Flashcard.Score.GOOD,
            Flashcard.Score.EASY,
        )

        self._pending_score = score
        self.state = Flashcard.State.SCORED_PENDING_APPROVAL

    def approve_pending_score(self):
        """Apply the staged rating through the normal scoring path.

        Routes through ``set_score`` so the rating gets the same
        treatment a manual rating would: scheduler advance, post-rating
        State.Review → SCORED, State.Learning/Relearning →
        AWAITING_REVEAL.
        """
        assert self.state == Flashcard.State.SCORED_PENDING_APPROVAL
        assert self._pending_score is not None
        score = self._pending_score
        self._pending_score = None
        self.set_score(score)

    def discard_pending_score(self):
        """Reject the staged rating. No FSRS rollback needed since
        nothing was applied. Latches ``_auto_score_discarded`` so the
        enter-default suppresses auto-scoring on the remainder of this
        attempt (mirrors ``_auto_scoring_failed``)."""
        assert self.state == Flashcard.State.SCORED_PENDING_APPROVAL
        self._pending_score = None
        self._auto_score_discarded = True
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

    def again(self):
        """Apply Rating.Again. In practice the resulting FSRS state is
        always Learning or Relearning, so this lands in AWAITING_REVEAL —
        but the routing decision is made inside ``_apply_rating`` based
        on the actual FSRS outcome, not the rating button.
        """
        assert self.state in [
            Flashcard.State.REVEALED_NOT_SCORED,
            Flashcard.State.REVEALED_PENDING_AUTO_SCORE
        ]
        self._apply_rating(Rating.Again, Flashcard.Score.AGAIN)

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
        auto_scorer: Any = None,
        scheduler: Scheduler | None = None,
        _auto_accept_auto_scores: bool = False,
    ):
        super().__init__()
        # ``session_factory`` is held only for the public ``commit()`` API,
        # which is never called internally — no DB I/O happens during the
        # session itself. FSRS state lives entirely in memory on each
        # Flashcard, mutated through ``self._scheduler``.
        self._session_factory = session_factory
        self._scheduler = scheduler if scheduler is not None else Scheduler()
        self._cards = [Flashcard(card, self._scheduler) for card in cards]
        self._current_card_index = 0

        self._auto_score_enabled = auto_score_enabled
        self._auto_scorer = auto_scorer
        self._auto_accept_auto_scores = _auto_accept_auto_scores

        # Internal state
        self.state = FlashcardReviewViewModel.State.START
        self._cancelled = False
        self._collapsed = False
        self._help_visible = False
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

    
    def on_key(self, event: events.Key) -> None:
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
        # START
        #   - enter -> begin
        #   - ctrl+c -> cancel
        assert self.state == FlashcardReviewViewModel.State.START

        # Remark: do we want to be .stop()ing here?
        if event.key == KEYBINDINGS[Action.BEGIN]:
            self.begin()
        elif event.key == KEYBINDINGS[Action.CANCEL]:
            self.cancel()
            #event.stop()

    def _on_key_reviewing(self, event: events.Key) -> None:
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

        if event.key == KEYBINDINGS[Action.PREV_CARD]:
            self.prev_card()

        elif event.key == KEYBINDINGS[Action.NEXT_CARD]:
            self.next_card()

        elif event.key == KEYBINDINGS[Action.CANCEL]:
            self.cancel()
            #event.stop()

        elif event.key == KEYBINDINGS[Action.TOGGLE_TIMER]:
            if self.current_card:
                self.current_card.toggle_timer_visible()
                self._emit(self.dirty)

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


        elif event.key in (score_keys := _score_key_map()):
            if self.current_card and self.current_card.state in [
                Flashcard.State.REVEALED_NOT_SCORED,
                Flashcard.State.REVEALED_PENDING_AUTO_SCORE
            ]:
                self.score_current_card(score_keys[event.key])

        # All four "enter" flavors in REVIEWING share the same key — keyed
        # off SCORE_DEFAULT here for the lookup; siblings REVEAL_BACK,
        # REVEAL_FRONT, ADVANCE_NEXT all map to the same key. The branch
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
                    # Default enter-on-revealed action depends on config and
                    # on whether this card has already failed auto-scoring.
                    if (
                        self._auto_score_enabled
                        and not self.current_card.auto_scoring_failed
                    ):
                        self.score_current_card(Flashcard.Score.AUTO)
                    else:
                        # Either auto-scoring is disabled, or this card has
                        # already burned its auto-score attempt — user needs
                        # to rate manually. Default to GOOD.
                        self.score_current_card(Flashcard.Score.GOOD)

                elif self.current_card.state in [
                    Flashcard.State.SCORED,
                    Flashcard.State.REVEALED_PENDING_AUTO_SCORE,
                ]:
                    # Card is already done (or queued for auto-score) — enter
                    # advances to the next card still needing attention.
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

    async def commit(self) -> None:
        """Persist every card's current FSRS scheduling state to the DB.

        Idempotent — calling this repeatedly writes the same state each
        time (assuming no further ratings happen in between). Never
        called internally; reserved for the widget's caller (typically
        the ``review_present_flashcards`` tool) to invoke when a session
        completes against a non-ephemeral review session.

        Cards that haven't been rated this session (SKIPPED cards left
        in FRONT, untouched cards from a cancelled session) still get
        their FSRS state written — but since ``_current_fsrs_card`` was
        never advanced for those, the write is a no-op equivalent.
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

        # Discard from BOTH queues — a card transitioning to SCORED must not leave a ghost id
        # in _next_remaining (e.g. a previously-AGAIN'd card that the user revealed and scored
        # before its due timer fired). The requeue branch below re-adds to _next afterwards
        # for cards that end up in Learning / Relearning.
        self._remaining_before_batched_autoscore.discard(self.current_card.id)
        self._next_remaining_before_batched_autoscore.discard(self.current_card.id)

        # AUTO and SKIPPED don't run the rating through the scheduler, so
        # there's no FSRS state to branch on — they're terminal-for-now
        # transitions handled directly.
        if score == Flashcard.Score.AUTO:
            self.current_card.set_score_auto()
            self._goto_next_unscored_card()

        elif score == Flashcard.Score.SKIPPED:
            self.current_card.skip()
            self._goto_next_unscored_card()

        # EASY/GOOD/HARD/AGAIN: apply the rating, then branch on the
        # post-rating FSRS state.
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
                # Emplace this card at the back of the _cards list. Note that
                # this implicitly moves the cursor: for middle/first positions
                # it shifts a new card into _current_card_index; for the last
                # position it leaves the cursor on the just-requeued card.
                self._cards.remove(current_card)
                self._cards.append(current_card)
                self._next_remaining_before_batched_autoscore.add(current_card.id)

            # Land on the next unscored card. If the implicit shift above
            # already placed us on an unscored card, _goto will stay put
            # and just start that card's timer.
            self._goto_next_unscored_card()

        self._emit(self.dirty)

        # Check if we need to trigger a batched auto-score, or transition
        # to DONE after scoring this card. Order matters: if a batch is
        # dispatched, _check_done correctly stays its hand because the
        # pending cards aren't terminally scored yet.
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

    def _check_ready_to_autoscore(self):
        """If the current round has drained and there are cards waiting on
        auto-scoring, swap in the next round and dispatch a batch.

        Round rollover is intentionally bundled here: by the time
        ``_remaining`` drains, every card the user has ratings-of-record
        for in this round either landed in SCORED (terminal) or in
        REVEALED_PENDING_AUTO_SCORE (waiting for the batch). The
        ``_next_remaining`` carry-over is the right input for the next
        round.
        """
        if self._remaining_before_batched_autoscore:
            return

        # Guard against re-entry while a batch is already running. The
        # running batch's completion handler will call back into this
        # method once it's done.
        if self.autoscore_in_progress:
            return

        # Round rollover: AGAIN'd / requeued cards from this round
        # become the next round's queue.
        self._remaining_before_batched_autoscore = self._next_remaining_before_batched_autoscore
        self._next_remaining_before_batched_autoscore = set()

        pending_auto_score = [c for c in self._cards if c.score == Flashcard.Score.AUTO]
        if not pending_auto_score:
            return

        self._autoscore_task = asyncio.create_task(self._handle_batched_auto_score(pending_auto_score))
        self._emit(self.dirty)

    def _check_done(self):
        """If every card is terminally scored (HARD/GOOD/EASY/SKIPPED in
        the SCORED state), transition to DONE.

        Pending AUTO cards (state == REVEALED_PENDING_AUTO_SCORE) and
        AWAITING_REVEAL cards both fail this check on their state alone,
        so no separate ``autoscore_in_progress`` guard is needed — by
        the time the last batch completes and its requeued/failed cards
        are placed, this check runs and either fires or doesn't on its
        own merits.
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
            # Whole-batch failure: revert every card that's still sitting in
            # REVEALED_PENDING_AUTO_SCORE (i.e. the scorer didn't get to
            # produce a rating for it before blowing up). Anything that
            # already made it through ``set_score`` / ``set_pending_score``
            # is already committed.
            requeued = []
            failed = []
            pending_approval = []
            for card in pending_auto_score:
                if card.state == Flashcard.State.REVEALED_PENDING_AUTO_SCORE:
                    card._revert_auto_score_failure()
                    failed.append(card)

        # Requeued cards (auto-accept mode, post-rating FSRS state in
        # Learning/Relearning) get moved to the back of _cards.
        for card in requeued:
            pos = self._cards.index(card)
            self._cards.pop(pos)
            if pos < self._current_card_index:
                self._current_card_index -= 1
            self._cards.append(card)
            # The card is already in AWAITING_REVEAL (set inside
            # Flashcard._apply_rating). Add it to THIS round's remaining
            # bucket — we already swapped _next into _remaining when the
            # scoring job was dispatched, so the requeued card is part of the
            # round we just opened.
            self._remaining_before_batched_autoscore.add(card.id)

        # Failed and pending-approval cards stay in place positionally.
        # Failed cards need manual rating; pending-approval cards need
        # user approval or discard. Either way, the round can't complete
        # until the user attends to them, so they belong in _remaining.
        for card in failed:
            self._remaining_before_batched_autoscore.add(card.id)
        for card in pending_approval:
            self._remaining_before_batched_autoscore.add(card.id)

        # The cursor may be sitting on a card that's now SCORED; advance it
        # to the next unscored card (if any).
        self._goto_next_unscored_card()

        # Clear the task handle _before_ any follow-up calls that might
        # observe ``autoscore_in_progress`` or try to cancel us.
        self._autoscore_task = None

        self._emit(self.dirty)

        # Pick up anything the user drained while the batch was running
        # (may dispatch a follow-up batch), then close the session if
        # every card is now terminally scored.
        self._check_ready_to_autoscore()
        self._check_done()


    async def _auto_score(
        self, pending_auto_score: list[Flashcard]
    ) -> tuple[list[Flashcard], list[Flashcard], list[Flashcard]]:
        """Batch-score every pending-auto card via the scorer subagent.

        For each card, stages the rating returned by the scorer for user
        approval via ``set_pending_score`` (currently always
        REQUIRE_APPROVAL behavior — the AutoScoreMode toggle that picks
        AUTO_ACCEPT vs REQUIRE_APPROVAL comes in a follow-up step).
        Every successful rating, including AGAIN, lands in
        SCORED_PENDING_APPROVAL — the user has the same say over a
        "model wants to see this again" call as a "model thinks you got
        it" call. No FSRS state advances and no DB commits.

        Returns a tuple ``(failed_cards, pending_approval_cards)``:

        - ``failed_cards``: cards the scorer couldn't score (dropped from
          the response, non-integer, or out-of-range). Reverted via
          ``Flashcard._revert_auto_score_failure`` — now
          ``REVEALED_NOT_SCORED`` with ``auto_scoring_failed == True``
          and need to go back into ``_remaining`` for manual rating.

        - ``pending_approval_cards``: successfully-rated cards now
          sitting in SCORED_PENDING_APPROVAL. Caller adds these to
          ``_remaining`` so the round stays open until the user
          approves or discards each one.

        Uses the stable flashcard id as ``flashcard_id`` in the prompt
        so results map back unambiguously, regardless of return order.
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

            if self._auto_accept_auto_scores:
                card.set_score(score_map[rating])
                if card.fsrs_card.state in (State.Learning, State.Relearning):
                    requeued_cards.append(card)
            else:
                card.set_pending_score(score_map[rating])
                pending_approval_cards.append(card)

        return requeued_cards, failed_cards, pending_approval_cards
