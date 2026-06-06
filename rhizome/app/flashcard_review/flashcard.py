"""Flashcard — per-card state machine for the FlashcardReview widget.

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
    SCORED_PENDING_APPROVAL
        The auto-scorer produced a rating for this card and the VM is in REQUIRE_APPROVAL
        mode, so the rating is staged in ``_pending_score`` awaiting user approval. FSRS
        state is intentionally NOT advanced while we wait — the rating is only applied if
        the user approves. Reached only from REVEALED_PENDING_AUTO_SCORE via the batch
        scorer's per-card ``set_pending_score()`` call.
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
        -> SCORED or AWAITING_REVEAL [via set_score(EASY|GOOD|HARD|AGAIN); only when the
                                      VM is in AUTO_ACCEPT mode, or when the user manually
                                      pre-empts the auto-score]
            - Either the user manually scored the card (any time), or the batch auto-scorer
              returned a rating in {1,2,3,4} while the VM is in AUTO_ACCEPT mode.
            - Manual override of a PENDING_AUTO card is blocked by
              ``FlashcardReviewModel`` while the auto-score task is in flight (the
              user's score would be immediately overridden once the batch returns).
            - Same routing as the REVEALED_NOT_SCORED variant: scheduler advances
              ``_current_fsrs_card``, then SCORED if Review or AWAITING_REVEAL if
              Learning/Relearning.

        -> SCORED_PENDING_APPROVAL [via set_pending_score(EASY|GOOD|HARD|AGAIN); only when
                                    the VM is in REQUIRE_APPROVAL mode]
            - The batch auto-scorer returned a rating in {1,2,3,4} and the VM is in
              REQUIRE_APPROVAL mode, so the rating is staged on ``_pending_score`` instead
              of being applied immediately.
            - FSRS state is NOT advanced — the scheduler is only invoked on approval.

        -> REVEALED_NOT_SCORED [via _revert_auto_score_failure()]
            - The batch scorer dropped this card, returned a non-integer / out-of-range
              score, or the whole batch raised.
            - Latches ``auto_scoring_failed=True``, which forbids future ``set_score_auto()``
              calls on this card until ``reset()`` is invoked.


    SCORED_PENDING_APPROVAL
        -> SCORED or AWAITING_REVEAL [via approve_pending_score(); equivalently via the VM's
                                      score_current_card() when the user presses enter or a
                                      digit key on a SCORED_PENDING_APPROVAL card]
            - The user approved the staged rating (enter), or rejected it and supplied a
              manual rating (1/2/3/4) — both paths clear ``_pending_score`` and route the
              chosen rating through the normal scoring path. Scheduler advances
              ``_current_fsrs_card``, then SCORED if Review or AWAITING_REVEAL if
              Learning/Relearning.

        -> REVEALED_NOT_SCORED [via discard_pending_score()]
            - The user rejected the staged rating without supplying a manual one (``d`` key).
            - Latches ``_auto_score_discarded=True`` (mirrors ``auto_scoring_failed`` —
              suppresses the enter-default's auto-score fallback so the user gets a manual
              GOOD instead of immediately re-deferring). FSRS state is unchanged since the
              staged rating was never applied. Cleared by ``reset()`` and by
              ``_reset_session_metadata()``.

        -> SCORED(SKIPPED) [via discard_pending_score() then skip(), driven by the VM's
                            alt+s handler]
            - The user skipped the card while it was awaiting approval. The VM discards the
              pending rating first (transitioning to REVEALED_NOT_SCORED) and then calls
              skip() from there. FSRS state is unchanged.


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
            - Clears ``_score``, ``_user_answer``, ``auto_scoring_failed``,
              ``_auto_score_discarded``, and ``_pending_score``; resets the think-time
              timer; cancels any in-flight ``_wait_until_due`` task; AND restores
              ``_current_fsrs_card`` to a copy of ``_initial_fsrs_card`` (this is the only
              operation that does so). Because no DB write has happened, this restoration
              is fully consistent.

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
    - ``_auto_score_discarded`` latches when the user rejects a SCORED_PENDING_APPROVAL
      rating via ``discard_pending_score()``. Same suppressing effect on the enter-default
      as ``auto_scoring_failed`` (manual GOOD fallback) but kept distinct so the view can
      surface "user rejected" vs "scorer broke" differently. Cleared by ``reset()`` and by
      ``_reset_session_metadata()``.

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
"""

import asyncio
import copy
from collections.abc import Callable
from datetime import UTC, datetime
from enum import Enum, auto
from typing import NotRequired, TypedDict

from fsrs import Card, Rating, Scheduler, State

from rhizome.app.flashcard_review.timer import Timer


# Simple dataclass constructed in the interrupt payload in the tool call. ``fsrs_card`` is the in-memory
# FSRS scheduling state for this card at the start of the session — built from the DB row by
# ``to_fsrs_card`` (or constructed directly for tests / sample data). The Flashcard takes a private copy so
# the caller's object isn't mutated.
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

        # FSRS scheduling state — managed entirely in memory. ``_initial`` is the snapshot at session start
        # (used by ``reset()`` to fully roll the card back); ``_current`` is mutated by every rating.
        # Neither is ever written to the DB during the session — the VM's ``commit()`` is the only
        # sanctioned write site, and it's called by the widget's owner, not internally.
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
        self._user_answer: str | None = None
        self._awaiting_reveal_task: asyncio.Task | None = None
        # Set to True if the auto-scorer has previously failed on this card (either returned an invalid
        # result or crashed the whole batch). Once set, the card cannot be re-deferred to the auto-scorer —
        # ``set_score_auto`` will assert and the FlashcardReview enter-path falls back to a manual GOOD
        # rating. Cleared by ``reset()``.
        self._auto_scoring_failed: bool = False
        # Set to True when the user discards a SCORED_PENDING_APPROVAL auto-score. Has the same
        # suppressing effect on subsequent auto-score attempts within the same attempt as
        # ``_auto_scoring_failed`` (the enter-path falls back to a manual rating), but kept as a separate
        # flag so the view can distinguish "scorer broke" from "user rejected" in its on-screen message.
        # Cleared by ``reset()`` and by ``_reset_session_metadata()``.
        self._auto_score_discarded: bool = False

        # The proposed rating from the auto-scorer that the user has not yet approved or discarded. Set by
        # ``set_pending_score`` while in REQUIRE_APPROVAL mode; consumed by ``approve_pending_score`` (which
        # then runs the rating through the normal scoring path); cleared by ``discard_pending_score``. The
        # FSRS state is NOT advanced while the rating sits here — discard is just clearing the field, no
        # rollback needed.
        self._pending_score: Flashcard.Score | None = None

        # User-toggled "flag for later" annotation. Orthogonal to all card-state machinery — toggleable from
        # any state, no transitions, doesn't affect scoring or session flow.
        self._flagged: bool = False

        # Fired from ``_wait_until_due`` after the card auto-reveals from AWAITING_REVEAL back to FRONT.
        # The VM wires this up to its ``dirty`` emit so listeners get notified of the async transition
        # (which otherwise bypasses all VM methods).
        self._on_due_reveal: Callable[[], None] | None = None

        # Cached per-rating "seconds-until-due" preview, computed once at the FRONT -> REVEALED_NOT_SCORED
        # transition. Cached because Scheduler.review_card is non-deterministic (fuzzing), so we don't want
        # the displayed intervals to fluctuate every render. Cleared whenever the FSRS state changes
        # (reset / requeue).
        self._cached_rating_previews: dict[Rating, float] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def score(self) -> Score | None:
        return self._score

    @property
    def pending_score(self) -> Score | None:
        """The auto-scorer's proposed rating, while the card sits in SCORED_PENDING_APPROVAL. ``None`` in
        any other state."""
        return self._pending_score


    @property
    def scored(self) -> bool:
        return self._score != None

    @property
    def elapsed_time(self) -> float:
        return self._timer.elapsed()

    @property
    def user_answer(self) -> str | None:
        return self._user_answer

    @property
    def auto_scoring_failed(self) -> bool:
        return self._auto_scoring_failed
    
    @property
    def auto_score_discarded(self) -> bool:
        return self._auto_score_discarded

    @property
    def flagged(self) -> bool:
        return self._flagged

    def toggle_flagged(self) -> None:
        """Flip the user's "flag for later" annotation. Orthogonal to card state — valid in any state."""
        self._flagged = not self._flagged

    @property
    def fsrs_card(self) -> Card:
        """The card's current in-memory FSRS scheduling state.

        Mutated by ``set_score`` / ``again`` (which run the rating through the VM-owned scheduler), and
        rolled back to the initial snapshot by ``reset()``. Never persisted unless the VM's caller invokes
        ``commit()`` on the view-model.
        """
        return self._current_fsrs_card

    def rating_previews(self) -> dict[Rating, float]:
        """Cached per-rating "seconds-until-due" preview against the card's current FSRS state.

        Populated by ``_compute_rating_previews`` at the FRONT -> REVEALED_NOT_SCORED transition (when the
        rating row first becomes visible). Cached because ``Scheduler.review_card`` is non-deterministic
        (fuzzing) — without caching, the displayed intervals would jitter on every render.
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
        """Resume the think-time timer. Only valid in FRONT (the only state where the timer should ever be
        running). Idempotent w.r.t. the underlying Timer."""
        assert self.state == Flashcard.State.FRONT
        self._timer.start()

    def pause(self):
        """Pause the think-time timer. Only valid in FRONT. Idempotent."""
        assert self.state == Flashcard.State.FRONT
        self._timer.pause()

    def _reset_session_metadata(self):
        """Clear per-attempt session metadata WITHOUT touching FSRS state.

        Used by both the public ``reset()`` and by ``again()`` (which funnels into AWAITING_REVEAL —
        rolling the FSRS state back there would discard the Rating.Again that's about to be applied).
        """
        self._timer.reset()
        self._score = None
        # The user gets a clean crack at auto-scoring on the next attempt, and the previously-typed draft
        # answer is cleared so the input shows empty when the card comes back around.
        self._auto_scoring_failed = False
        self._auto_score_discarded = False
        self._user_answer = None
        # Per-rating preview cache is per-attempt — drop it so the next reveal_back recomputes against
        # whatever FSRS state is current by then.
        self._cached_rating_previews = None

        if self._awaiting_reveal_task is not None:
            self._awaiting_reveal_task.cancel()
            self._awaiting_reveal_task = None

    def reset(self):
        """Fully reset the card: clear session metadata AND restore FSRS state to the session's initial
        snapshot.

        Called when the user explicitly opts to retry the card from scratch (alt+r, or unskip via alt+s on
        a SKIPPED card). Because FSRS state hasn't been committed mid-session, restoring
        ``_current_fsrs_card`` to ``_initial_fsrs_card`` is fully consistent — the next rating starts from
        a true initial state.
        """
        self._reset_session_metadata()
        self._current_fsrs_card = copy.copy(self._initial_fsrs_card)
        # If the card was sitting in SCORED_PENDING_APPROVAL, drop the staged rating along with everything
        # else.
        self._pending_score = None
        self.state = Flashcard.State.FRONT

    def reveal_back(self):
        assert self.state == Flashcard.State.FRONT
        # Stop timing "think time" — user has committed to revealing.
        self.pause()
        self.state = Flashcard.State.REVEALED_NOT_SCORED
        # Lock in the per-rating previews now that the rating row is about to become visible. See
        # _cached_rating_previews.
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

        # Back to FRONT — transition state first, then resume the timer (self.unpause requires state == FRONT).
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
            # Auto-scorer scoring the card, or user manually scoring a card that was pending auto-score.
            Flashcard.State.REVEALED_PENDING_AUTO_SCORE,
            # User approving a pending auto-score (via approve_pending_score).
            Flashcard.State.SCORED_PENDING_APPROVAL,
        ]

        self._apply_rating(Rating(score.value), score)

    def _apply_rating(self, rating: Rating, score: Score) -> None:
        """Run the rating through the scheduler and route to either SCORED or AWAITING_REVEAL based on the
        resulting FSRS state.

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

        # Still inside the (re)learning step ladder — requeue the card via the due timer.
        # ``_reset_session_metadata`` clears _score (consistent with AWAITING_REVEAL), drops the
        # user_answer draft, clears the auto_scoring_failed latch, and resets the think-time timer for the
        # next FRONT cycle. It does NOT touch FSRS state.
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
        """Friend-method used by ``FlashcardReview._auto_score`` when the scorer fails to produce a valid
        rating for this card.

        Rolls the card back from ``REVEALED_PENDING_AUTO_SCORE`` to ``REVEALED_NOT_SCORED`` and clears the
        AUTO score, while latching ``auto_scoring_failed`` so the user can't re-defer this card (they must
        rate it manually, or ``reset()`` it to try again).
        """
        assert self.state == Flashcard.State.REVEALED_PENDING_AUTO_SCORE
        self._score = None
        self._auto_scoring_failed = True
        self.state = Flashcard.State.REVEALED_NOT_SCORED

    def set_pending_score(self, score: Score):
        """Stage a rating from the auto-scorer for user approval.

        Used by the batch auto-scorer when the VM is in REQUIRE_APPROVAL mode. Records the proposed rating
        in ``_pending_score`` and transitions to SCORED_PENDING_APPROVAL. FSRS state is intentionally
        untouched — the rating is only applied if the user approves, so ``discard_pending_score`` is a pure
        field-clearing operation.

        All four FSRS ratings (AGAIN/HARD/GOOD/EASY) are valid — the user gets the same approval gate over
        the model's call regardless of which way it went. SKIPPED is a user-only action and not a valid
        auto-score outcome.
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

        Routes through ``set_score`` so the rating gets the same treatment a manual rating would:
        scheduler advance, post-rating State.Review → SCORED, State.Learning/Relearning → AWAITING_REVEAL.
        """
        assert self.state == Flashcard.State.SCORED_PENDING_APPROVAL
        assert self._pending_score is not None
        score = self._pending_score
        self._pending_score = None
        self.set_score(score)

    def discard_pending_score(self):
        """Reject the staged rating. No FSRS rollback needed since nothing was applied. Latches
        ``_auto_score_discarded`` so the enter-default suppresses auto-scoring on the remainder of this
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
        """Apply Rating.Again. In practice the resulting FSRS state is always Learning or Relearning, so
        this lands in AWAITING_REVEAL — but the routing decision is made inside ``_apply_rating`` based on
        the actual FSRS outcome, not the rating button.
        """
        assert self.state in [
            Flashcard.State.REVEALED_NOT_SCORED,
            Flashcard.State.REVEALED_PENDING_AUTO_SCORE,
            # User approving a staged AGAIN auto-score, or rejecting a non-AGAIN auto-score and manually
            # rating AGAIN. The dispatcher clears _pending_score before calling set_score, so reaching
            # again() from SCORED_PENDING_APPROVAL is well-formed.
            Flashcard.State.SCORED_PENDING_APPROVAL,
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
