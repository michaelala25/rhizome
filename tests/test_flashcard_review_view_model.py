"""Tests for rhizome.tui.widgets.flashcard_review.view_model.

Covers the ``Timer``, ``Flashcard``, and ``FlashcardReviewViewModel`` classes.
FSRS state lives entirely in memory now — no DB stubbing is required for the
rating path. The shared ``recording_scheduler`` fixture wraps a real
``fsrs.Scheduler`` so tests can assert rating mappings while still exercising
the actual scheduler's state transitions.

The default ``_starter_data`` cards start in ``State.Review`` (already
graduated from the Learning ladder) — that way HARD/GOOD/EASY cleanly land
in SCORED and only AGAIN goes to Relearning → AWAITING_REVEAL, matching the
behavior the bulk of these tests were originally written against.
``TestLearningLadder`` covers the new "stay in Learning/Relearning →
AWAITING_REVEAL" behavior using ``_learning_starter_data``.

The ``_wait_until_due`` tasks spawned by the AWAITING_REVEAL transitions
sleep for the FSRS-computed delay (60s+ with default config), so they never
fire during a test — the ``review`` fixture cancels any lingering tasks at
teardown.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fsrs import Card, Rating, Scheduler, State

from rhizome.tui.widgets.flashcard_review.view_model import (
    Flashcard,
    FlashcardReviewViewModel,
    Timer,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def commit(self):
        return None

    async def get(self, *_, **__):
        # Only ever reached if a test invokes vm.commit() — which the
        # default tests don't. Returns None so commit_fsrs_card raises a
        # clear error rather than silently corrupting state.
        return None


def _fake_session_factory():
    return _FakeSession()


class _RecordingScheduler:
    """Wraps a real ``fsrs.Scheduler`` and records every rating call.

    Used in tests that want to assert "GOOD was applied to card 42" without
    coupling to internal Scheduler state. Forwards to the real scheduler so
    ``due`` values reflect actual FSRS scheduling (which the AGAIN /
    AWAITING_REVEAL flow depends on).
    """

    # The four ratings, in the order Flashcard._compute_rating_previews
    # iterates them — used to detect & strip preview batches from the
    # recorded call log.
    _PREVIEW_BATCH = (Rating.Again, Rating.Hard, Rating.Good, Rating.Easy)

    def __init__(self):
        self._real = Scheduler()
        self.calls: list[tuple[int, Rating]] = []

    def review_card(self, card, rating, review_dt):
        self.calls.append((card.card_id, rating))
        return self._real.review_card(card, rating, review_dt)

    @property
    def scoring_calls(self) -> list[tuple[int, Rating]]:
        """``self.calls`` minus the preview batches that ``reveal_back``
        emits. A preview batch is 4 sequential calls with the same
        card_id and ratings ``(Again, Hard, Good, Easy)`` in order.
        """
        result = list(self.calls)
        i = 0
        while i <= len(result) - 4:
            window = result[i:i + 4]
            card_ids = {c for c, _ in window}
            ratings = tuple(r for _, r in window)
            if len(card_ids) == 1 and ratings == self._PREVIEW_BATCH:
                del result[i:i + 4]
            else:
                i += 1
        return result


@pytest.fixture
def recording_scheduler() -> _RecordingScheduler:
    return _RecordingScheduler()


def _starter_data(card_id: int, *, q: str = "", a: str = "") -> dict:
    """Build a FlashcardData dict with a graduated (Review-state) FSRS card.

    HARD/GOOD/EASY on a Review-state card stays in Review (→ SCORED);
    AGAIN drops to Relearning (→ AWAITING_REVEAL). This matches the
    behavioral expectations of the bulk of the test suite. Use
    ``_learning_starter_data`` for tests that exercise the
    Learning-ladder requeue path.
    """
    return {
        "id": card_id,
        "question": q or f"q{card_id}",
        "answer": a or f"a{card_id}",
        "fsrs_card": Card(
            card_id=card_id,
            state=State.Review,
            step=None,
            stability=10.0,
            difficulty=5.0,
            due=datetime.now(UTC),
        ),
    }


def _learning_starter_data(card_id: int) -> dict:
    """Build a FlashcardData dict with a fresh Learning-state FSRS card.

    Used by tests that intentionally exercise the Learning-ladder
    requeue behavior (HARD/GOOD on a Learning-step card stays in
    Learning, lands in AWAITING_REVEAL).
    """
    return {
        "id": card_id,
        "question": f"q{card_id}",
        "answer": f"a{card_id}",
        "fsrs_card": Card(card_id=card_id),
    }


@pytest.fixture
def card_data() -> list[dict]:
    return [_starter_data(1), _starter_data(2), _starter_data(3)]


@pytest.fixture
def card(recording_scheduler) -> Flashcard:
    return Flashcard(_starter_data(42, q="q", a="a"), recording_scheduler)


@pytest.fixture
async def review(card_data, recording_scheduler):
    r = FlashcardReviewViewModel(
        cards=card_data,
        session_factory=_fake_session_factory,
        auto_score_enabled=True,
        auto_scorer=None,
        scheduler=recording_scheduler,
    )
    yield r
    # Teardown: cancel any lingering async tasks so they don't leak
    for c in r._cards:
        if c._awaiting_reveal_task is not None and not c._awaiting_reveal_task.done():
            c._awaiting_reveal_task.cancel()
    if r._autoscore_task is not None and not r._autoscore_task.done():
        r._autoscore_task.cancel()


class _FakeScorer:
    """Mimics a StructuredSubagent for tests.

    Constructed with a dict mapping ``flashcard_id -> rating (1-4)``. On
    ``ainvoke``, populates ``structured_response`` with matching results for
    every id in the dict."""

    def __init__(self, results_by_id: dict[int, int]):
        self._results_by_id = results_by_id
        self.structured_response = None
        self.invocations = 0

    async def ainvoke(self, prompt: str):
        self.invocations += 1
        results = [
            SimpleNamespace(flashcard_id=fc_id, score=score, feedback="")
            for fc_id, score in self._results_by_id.items()
        ]
        self.structured_response = SimpleNamespace(results=results)


# ---------------------------------------------------------------------------
# Timer
# ---------------------------------------------------------------------------


class TestTimer:
    def test_initial_state(self):
        t = Timer()
        assert not t.running
        assert not t.started
        assert t.elapsed() == 0.0

    def test_start_makes_running(self):
        t = Timer()
        t.start()
        assert t.running

    def test_start_is_idempotent_when_running(self):
        t = Timer()
        t.start()
        t.start()
        assert t.running

    def test_pause_stops_running(self):
        t = Timer()
        t.start()
        t.pause()
        assert not t.running

    def test_pause_is_noop_when_not_running(self):
        t = Timer()
        t.pause()
        assert not t.running
        t.start()
        t.pause()
        t.pause()  # second pause is a no-op
        assert not t.running

    def test_start_after_pause_resumes(self):
        t = Timer()
        t.start()
        t.pause()
        elapsed_at_pause = t.elapsed()
        t.start()
        assert t.running
        # Accumulated time is preserved across pause/resume
        assert t.elapsed() >= elapsed_at_pause

    def test_stop_finalizes_and_returns_total(self):
        t = Timer()
        t.start()
        total = t.stop()
        assert not t.running
        assert total >= 0.0

    def test_stop_is_idempotent(self):
        t = Timer()
        t.start()
        first = t.stop()
        second = t.stop()
        assert first == second

    def test_stop_when_never_started_is_noop(self):
        t = Timer()
        total = t.stop()
        assert total == 0.0

    def test_start_after_stop_raises(self):
        """Stop is terminal — user must explicitly reset() to restart."""
        t = Timer()
        t.start()
        t.stop()
        with pytest.raises(RuntimeError):
            t.start()

    def test_reset_after_stop_allows_restart(self):
        t = Timer()
        t.start()
        t.stop()
        t.reset()
        t.start()
        assert t.running

    def test_elapsed_only_accrues_while_running(self):
        t = Timer()
        t.start()
        # Give it a moment to accrue
        import time as _t
        _t.sleep(0.01)
        t.pause()
        paused = t.elapsed()
        _t.sleep(0.01)
        # Paused time shouldn't grow
        assert t.elapsed() == paused


# ---------------------------------------------------------------------------
# Flashcard — transitions
# ---------------------------------------------------------------------------


class TestFlashcardBasics:
    def test_initial_state(self, card):
        assert card.state == Flashcard.State.FRONT
        assert card.score is None
        assert not card.scored
        assert not card._timer.running

    def test_unpause_starts_timer_in_front(self, card):
        card.unpause()
        assert card._timer.running

    def test_unpause_is_idempotent(self, card):
        card.unpause()
        card.unpause()
        assert card._timer.running

    def test_pause_outside_front_asserts(self, card):
        card.unpause()
        card.reveal_back()  # state → REVEALED_NOT_SCORED
        with pytest.raises(AssertionError):
            card.pause()

    def test_unpause_outside_front_asserts(self, card):
        card.unpause()
        card.reveal_back()
        with pytest.raises(AssertionError):
            card.unpause()

    def test_set_user_answer(self, card):
        card.set_user_answer("my answer")
        assert card.user_answer == "my answer"

    def test_set_user_answer_only_in_front(self, card):
        card.unpause()
        card.reveal_back()
        with pytest.raises(AssertionError):
            card.set_user_answer("too late")


class TestFlashcardReveal:
    def test_reveal_back_pauses_and_transitions(self, card):
        card.unpause()
        assert card._timer.running
        card.reveal_back()
        assert card.state == Flashcard.State.REVEALED_NOT_SCORED
        assert not card._timer.running

    async def test_reveal_front_from_awaiting_reveal(self, card):
        # async so ``again()``'s create_task has a running loop, even
        # though there's nothing to await directly.
        card.unpause()
        card.reveal_back()
        card.again()
        assert card.state == Flashcard.State.AWAITING_REVEAL
        card.reveal_front()
        assert card.state == Flashcard.State.FRONT
        assert card._timer.running
        # Awaiting-reveal task is cleaned up
        assert card._awaiting_reveal_task is None


class TestFlashcardScoring:
    def test_set_score_good_applies_rating(self, card, recording_scheduler):
        card.unpause()
        card.reveal_back()
        card.set_score(Flashcard.Score.GOOD)
        assert card.state == Flashcard.State.SCORED
        assert card.score == Flashcard.Score.GOOD
        assert recording_scheduler.scoring_calls == [(42, Rating.Good)]

    def test_set_score_hard_maps_to_rating_hard(self, card, recording_scheduler):
        card.unpause()
        card.reveal_back()
        card.set_score(Flashcard.Score.HARD)
        assert recording_scheduler.scoring_calls == [(42, Rating.Hard)]

    def test_set_score_easy_maps_to_rating_easy(self, card, recording_scheduler):
        card.unpause()
        card.reveal_back()
        card.set_score(Flashcard.Score.EASY)
        assert recording_scheduler.scoring_calls == [(42, Rating.Easy)]

    def test_set_score_from_pending_auto_also_works(self, card):
        """The batch scorer calls set_score on a PENDING_AUTO card — the
        assertion must accept that state."""
        card.unpause()
        card.reveal_back()
        card.set_score_auto()
        assert card.state == Flashcard.State.REVEALED_PENDING_AUTO_SCORE
        card.set_score(Flashcard.Score.GOOD)
        assert card.state == Flashcard.State.SCORED
        assert card.score == Flashcard.Score.GOOD

    def test_set_score_auto_transitions(self, card):
        card.unpause()
        card.reveal_back()
        card.set_score_auto()
        assert card.state == Flashcard.State.REVEALED_PENDING_AUTO_SCORE
        assert card.score == Flashcard.Score.AUTO
        assert not card._timer.running

    async def test_set_score_delegates_again(self, card):
        card.unpause()
        card.reveal_back()
        card.set_score(Flashcard.Score.AGAIN)
        assert card.state == Flashcard.State.AWAITING_REVEAL
        card._awaiting_reveal_task.cancel()

    def test_set_score_delegates_skipped(self, card):
        card.unpause()
        card.reveal_back()
        card.set_score(Flashcard.Score.SKIPPED)
        assert card.state == Flashcard.State.SCORED
        assert card.score == Flashcard.Score.SKIPPED


class TestFlashcardAgainAndSkip:
    async def test_again_spawns_awaiting_reveal(self, card, recording_scheduler):
        card.unpause()
        card.reveal_back()
        card.again()
        assert card.state == Flashcard.State.AWAITING_REVEAL
        assert card._awaiting_reveal_task is not None
        assert card._due_timer is not None and card._due_timer.running
        assert recording_scheduler.scoring_calls == [(42, Rating.Again)]
        card._awaiting_reveal_task.cancel()

    async def test_again_from_pending_auto(self, card):
        """The batch scorer calls again() on a PENDING_AUTO card — the
        widened assertion must accept that state too."""
        card.unpause()
        card.reveal_back()
        card.set_score_auto()
        card.again()
        assert card.state == Flashcard.State.AWAITING_REVEAL
        card._awaiting_reveal_task.cancel()

    def test_skip_from_revealed(self, card):
        card.unpause()
        card.reveal_back()
        card.skip()
        assert card.state == Flashcard.State.SCORED
        assert card.score == Flashcard.Score.SKIPPED

    async def test_skip_from_awaiting_reveal_cancels_task(self, card):
        card.unpause()
        card.reveal_back()
        card.again()
        task = card._awaiting_reveal_task
        card.skip()
        assert card.state == Flashcard.State.SCORED
        assert card.score == Flashcard.Score.SKIPPED
        # Allow the cancelled task a tick to actually finalize
        await asyncio.sleep(0)
        assert task.cancelled() or task.done()

    def test_reset_clears_score_state_and_timer(self, card):
        card.unpause()
        card.reveal_back()
        card.skip()
        card.reset()
        assert card.state == Flashcard.State.FRONT
        assert card.score is None
        assert not card._timer.running


class TestFlashcardAutoScoreFailure:
    def test_revert_from_pending_auto(self, card):
        card.unpause()
        card.reveal_back()
        card.set_score_auto()
        card._revert_auto_score_failure()
        assert card.state == Flashcard.State.REVEALED_NOT_SCORED
        assert card.score is None
        assert card.auto_scoring_failed

    def test_set_score_auto_blocked_after_failure(self, card):
        card.unpause()
        card.reveal_back()
        card.set_score_auto()
        card._revert_auto_score_failure()
        # After failure the card is back in REVEALED_NOT_SCORED but
        # re-deferring is forbidden.
        assert card.state == Flashcard.State.REVEALED_NOT_SCORED
        with pytest.raises(AssertionError):
            card.set_score_auto()

    def test_reset_clears_auto_scoring_failed(self, card):
        card.unpause()
        card.reveal_back()
        card.set_score_auto()
        card._revert_auto_score_failure()
        assert card.auto_scoring_failed
        card.reset()
        assert not card.auto_scoring_failed
        # And set_score_auto is allowed again after the reset
        card.unpause()
        card.reveal_back()
        card.set_score_auto()  # no error
        assert card.state == Flashcard.State.REVEALED_PENDING_AUTO_SCORE


# ---------------------------------------------------------------------------
# FlashcardReviewViewModel — lifecycle and navigation
# ---------------------------------------------------------------------------


class TestReviewLifecycle:
    def test_initial_state(self, review):
        assert review.state == FlashcardReviewViewModel.State.START
        assert review.current_card is None
        assert review._remaining_before_batched_autoscore == {1, 2, 3}
        assert review._next_remaining_before_batched_autoscore == set()

    def test_begin_transitions_and_starts_first_timer(self, review):
        review.begin()
        assert review.state == FlashcardReviewViewModel.State.REVIEWING
        assert review.current_card.id == 1
        assert review.current_card._timer.running

    def test_cancel_transitions_to_done(self, review):
        review.begin()
        review.cancel()
        assert review.state == FlashcardReviewViewModel.State.DONE
        assert review.cancelled
        assert review.collapsed

    def test_finish_pauses_running_timer(self, review):
        review.begin()
        assert review.current_card._timer.running
        review.finish()
        assert review.state == FlashcardReviewViewModel.State.DONE
        # The first card's timer is no longer running after finish
        assert not review._cards[0]._timer.running

    async def test_finish_skips_any_awaiting_reveal_cards(self, review):
        review.begin()
        review.current_card.reveal_back()
        review.score_current_card(Flashcard.Score.AGAIN)
        # Card 1 is now in AWAITING_REVEAL
        assert review._cards[-1].id == 1
        assert review._cards[-1].state == Flashcard.State.AWAITING_REVEAL
        review.finish()
        # All AWAITING_REVEAL cards become SCORED with score=SKIPPED
        for c in review._cards:
            if c.id == 1:
                assert c.state == Flashcard.State.SCORED
                assert c.score == Flashcard.Score.SKIPPED


class TestReviewNavigation:
    def test_next_card_pauses_old_unpauses_new(self, review):
        review.begin()
        first = review.current_card
        review.next_card()
        assert not first._timer.running
        assert review.current_card._timer.running
        assert review.current_card.id == 2

    def test_prev_card_wraps_around(self, review):
        review.begin()
        review.prev_card()
        assert review.current_card.id == 3

    def test_next_card_wraps_around(self, review):
        review.begin()
        review.next_card()
        review.next_card()
        review.next_card()
        assert review.current_card.id == 1

    def test_navigation_over_non_front_card_no_timer_error(self, review):
        """Navigating onto a SCORED or AWAITING_REVEAL card must not try to
        unpause a non-FRONT card (would trigger the state assertion)."""
        review.begin()
        # Skip card 1 so it's SCORED
        review.current_card.reveal_back()
        review.current_card.skip()
        # Cursor is still at 1 (score_current_card wasn't used), but the card
        # is now SCORED. Navigate forward/back — must not raise.
        review.next_card()
        review.prev_card()
        # Card 1 is SCORED; its timer is not running (guard skipped unpause)
        assert not review._cards[0]._timer.running


# ---------------------------------------------------------------------------
# FlashcardReviewViewModel — scoring flow
# ---------------------------------------------------------------------------


class TestReviewScoring:
    async def test_score_good_advances_and_updates_remaining(self, review):
        review.begin()
        review.current_card.reveal_back()
        review.score_current_card(Flashcard.Score.GOOD)
        assert 1 not in review._remaining_before_batched_autoscore
        assert review.current_card.id == 2
        assert review._cards[0].state == Flashcard.State.SCORED

    async def test_score_all_good_finishes_session(self, review):
        review.begin()
        for _ in range(3):
            review.current_card.reveal_back()
            review.score_current_card(Flashcard.Score.GOOD)
        assert review.state == FlashcardReviewViewModel.State.DONE
        assert not review.cancelled

    async def test_again_middle_card_keeps_cursor_on_shifted_card(self, review):
        review.begin()
        review.next_card()  # → card 2
        assert review.current_card.id == 2
        review.current_card.reveal_back()
        review.score_current_card(Flashcard.Score.AGAIN)
        # List reordered: [1, 3, 2] (2 moved to end)
        assert [c.id for c in review._cards] == [1, 3, 2]
        # Cursor still at index 1 → card 3 (shifted into place)
        assert review.current_card.id == 3
        # Card 2 queued for next round, in AWAITING_REVEAL
        assert 2 in review._next_remaining_before_batched_autoscore
        assert review._cards[2].state == Flashcard.State.AWAITING_REVEAL

    async def test_again_first_card_keeps_cursor_on_shifted_card(self, review):
        review.begin()
        assert review.current_card.id == 1
        review.current_card.reveal_back()
        review.score_current_card(Flashcard.Score.AGAIN)
        # List: [2, 3, 1]; cursor at index 0 → card 2
        assert [c.id for c in review._cards] == [2, 3, 1]
        assert review.current_card.id == 2

    async def test_again_last_card_advances_past_itself(self, review):
        review.begin()
        review.next_card()
        review.next_card()
        assert review.current_card.id == 3
        review.current_card.reveal_back()
        review.score_current_card(Flashcard.Score.AGAIN)
        # List: [1, 2, 3] (3 removed from end, appended back — unchanged)
        # Cursor was at 2 (the AGAIN'd card), which is in next_remaining (not
        # remaining), so _goto walks forward. Wraps to 0 → card 1.
        assert [c.id for c in review._cards] == [1, 2, 3]
        assert review.current_card.id == 1

    async def test_round_swap_after_all_again(self, review):
        review.begin()
        for _ in range(3):
            review.current_card.reveal_back()
            review.score_current_card(Flashcard.Score.AGAIN)
        # All three AGAIN'd → drained remaining → swap next_remaining → remaining
        assert review._remaining_before_batched_autoscore == {1, 2, 3}
        assert review._next_remaining_before_batched_autoscore == set()
        # Still in REVIEWING — user must re-rate in the new round
        assert review.state == FlashcardReviewViewModel.State.REVIEWING
        # All cards in AWAITING_REVEAL (due timers waiting)
        for c in review._cards:
            assert c.state == Flashcard.State.AWAITING_REVEAL

    async def test_mixed_round_then_finish(self, review):
        """Rate card 1 AGAIN, then cards 2 and 3 GOOD, then re-rate card 1
        GOOD in the next round — session finishes.

        Note that after the round swap the cursor stays on the last-scored
        card (card 3) rather than auto-jumping to the new round. The user
        navigates manually to find the card 1 that was requeued."""
        review.begin()
        # Rate card 1 AGAIN → list [2, 3, 1], cursor on card 2
        review.current_card.reveal_back()
        review.score_current_card(Flashcard.Score.AGAIN)
        assert review.current_card.id == 2
        # Rate card 2 GOOD, cursor advances to card 3
        review.current_card.reveal_back()
        review.score_current_card(Flashcard.Score.GOOD)
        assert review.current_card.id == 3
        # Rate card 3 GOOD → remaining drains, round swaps to {1}
        review.current_card.reveal_back()
        review.score_current_card(Flashcard.Score.GOOD)
        assert review._remaining_before_batched_autoscore == {1}
        # Navigate to card 1 (at the end of the list, in AWAITING_REVEAL)
        while review.current_card.id != 1:
            review.next_card()
        assert review.current_card.state == Flashcard.State.AWAITING_REVEAL
        # Simulate user pressing enter to reveal_front, then reveal_back, then rate
        review.current_card.reveal_front()
        review.current_card.reveal_back()
        review.score_current_card(Flashcard.Score.GOOD)
        assert review.state == FlashcardReviewViewModel.State.DONE


# ---------------------------------------------------------------------------
# Learning / Relearning ladder behavior
# ---------------------------------------------------------------------------
#
# The branching invariant under test: after a rating is applied, the card lands
# in SCORED iff the post-rating FSRS state is Review; otherwise (Learning or
# Relearning) it lands in AWAITING_REVEAL and the VM requeues it just like an
# AGAIN. AGAIN is the degenerate case (Again never produces Review); HARD/GOOD
# on a card still inside the Learning step ladder is the new case.


class TestLearningLadder:
    async def test_good_on_learning_card_lands_in_awaiting_reveal(
        self, recording_scheduler
    ):
        """A fresh Learning-state card rated GOOD stays in Learning per
        FSRS, so the card lands in AWAITING_REVEAL (not SCORED) and the
        VM requeues it."""
        card = Flashcard(_learning_starter_data(7), recording_scheduler)
        card.unpause()
        card.reveal_back()
        card.set_score(Flashcard.Score.GOOD)
        assert card.fsrs_card.state == State.Learning
        assert card.state == Flashcard.State.AWAITING_REVEAL
        assert card.score is None  # cleared on the AWAITING_REVEAL branch
        assert card._awaiting_reveal_task is not None
        card._awaiting_reveal_task.cancel()

    async def test_hard_on_learning_card_lands_in_awaiting_reveal(
        self, recording_scheduler
    ):
        card = Flashcard(_learning_starter_data(7), recording_scheduler)
        card.unpause()
        card.reveal_back()
        card.set_score(Flashcard.Score.HARD)
        assert card.fsrs_card.state == State.Learning
        assert card.state == Flashcard.State.AWAITING_REVEAL
        card._awaiting_reveal_task.cancel()

    def test_easy_on_learning_card_graduates_to_scored(
        self, recording_scheduler
    ):
        """EASY on a fresh Learning card graduates straight to Review →
        the card lands in SCORED."""
        card = Flashcard(_learning_starter_data(7), recording_scheduler)
        card.unpause()
        card.reveal_back()
        card.set_score(Flashcard.Score.EASY)
        assert card.fsrs_card.state == State.Review
        assert card.state == Flashcard.State.SCORED
        assert card.score == Flashcard.Score.EASY

    async def test_good_then_due_then_good_walks_the_ladder_to_graduation(
        self, recording_scheduler
    ):
        """GOOD on Learning step 0 → step 1 (still Learning, requeued).
        After the due timer fires (simulated via reveal_front), another
        GOOD graduates the card."""
        card = Flashcard(_learning_starter_data(7), recording_scheduler)
        card.unpause()
        card.reveal_back()
        card.set_score(Flashcard.Score.GOOD)
        assert card.fsrs_card.state == State.Learning
        assert card.fsrs_card.step == 1
        assert card.state == Flashcard.State.AWAITING_REVEAL

        # Simulate the due timer firing (the user could also pre-empt
        # via reveal_front — same end state).
        card._awaiting_reveal_task.cancel()
        card.reveal_front()
        assert card.state == Flashcard.State.FRONT

        # Round 2: GOOD on Learning step 1 → graduates to Review.
        card.reveal_back()
        card.set_score(Flashcard.Score.GOOD)
        assert card.fsrs_card.state == State.Review
        assert card.state == Flashcard.State.SCORED

    async def test_vm_requeues_learning_card_to_next_remaining(self, recording_scheduler):
        """At the VM level, a Learning-state card scored GOOD must be
        treated identically to an AGAIN: emplaced at the back of
        ``_cards`` and added to ``_next_remaining``."""
        cards = [_learning_starter_data(1), _learning_starter_data(2), _learning_starter_data(3)]
        review = FlashcardReviewViewModel(
            cards=cards,
            session_factory=_fake_session_factory,
            auto_score_enabled=False,
            auto_scorer=None,
            scheduler=recording_scheduler,
        )
        try:
            review.begin()
            review.current_card.reveal_back()
            review.score_current_card(Flashcard.Score.GOOD)

            # Card 1 is now in Learning step 1 → AWAITING_REVEAL, moved
            # to the back of _cards, added to _next_remaining.
            assert [c.id for c in review._cards] == [2, 3, 1]
            assert review._cards[2].state == Flashcard.State.AWAITING_REVEAL
            assert 1 in review._next_remaining_before_batched_autoscore
            assert 1 not in review._remaining_before_batched_autoscore
            # Cursor implicitly shifts onto card 2 (slid into index 0).
            assert review.current_card.id == 2
        finally:
            for c in review._cards:
                if c._awaiting_reveal_task and not c._awaiting_reveal_task.done():
                    c._awaiting_reveal_task.cancel()

    async def test_batch_auto_score_requeues_learning_outcomes(self, recording_scheduler):
        """The batch auto-scorer's ``requeued_cards`` partition must
        include any card whose post-rating FSRS state is Learning or
        Relearning — not just AGAIN-rated cards."""
        # Three Learning-state cards. Scorer rates them all GOOD, which
        # keeps them in Learning → all three should be requeued.
        cards = [_learning_starter_data(1), _learning_starter_data(2), _learning_starter_data(3)]
        review = FlashcardReviewViewModel(
            cards=cards,
            session_factory=_fake_session_factory,
            auto_score_enabled=True,
            auto_scorer=_FakeScorer({1: 3, 2: 3, 3: 3}),
            scheduler=recording_scheduler,
        )
        try:
            review.begin()
            for _ in range(3):
                review.current_card.reveal_back()
                review.score_current_card(Flashcard.Score.AUTO)
            await review._autoscore_task

            # All three rated GOOD by the batch but stayed in Learning,
            # so all three are requeued: AWAITING_REVEAL, in _remaining
            # (current round, post-swap).
            for c in review._cards:
                assert c.state == Flashcard.State.AWAITING_REVEAL
                assert c.fsrs_card.state == State.Learning
            assert review._remaining_before_batched_autoscore == {1, 2, 3}
            assert review.state == FlashcardReviewViewModel.State.REVIEWING
        finally:
            for c in review._cards:
                if c._awaiting_reveal_task and not c._awaiting_reveal_task.done():
                    c._awaiting_reveal_task.cancel()
            if review._autoscore_task and not review._autoscore_task.done():
                review._autoscore_task.cancel()

    async def test_again_on_review_card_drops_to_relearning_then_requeues(
        self, recording_scheduler
    ):
        """AGAIN on a graduated (Review) card pushes it to Relearning,
        which is the other case for the AWAITING_REVEAL branch
        (alongside Learning). The VM should requeue identically."""
        card = Flashcard(_starter_data(7), recording_scheduler)  # Review-state
        card.unpause()
        card.reveal_back()
        card.set_score(Flashcard.Score.AGAIN)
        assert card.fsrs_card.state == State.Relearning
        assert card.state == Flashcard.State.AWAITING_REVEAL
        card._awaiting_reveal_task.cancel()


# ---------------------------------------------------------------------------
# FlashcardReviewViewModel — batch auto-scoring
# ---------------------------------------------------------------------------


class TestReviewBatchAutoScore:
    async def test_all_auto_all_good_triggers_batch_and_finishes(self, review):
        review._auto_scorer = _FakeScorer({1: 3, 2: 3, 3: 3})
        review.begin()
        for _ in range(3):
            review.current_card.reveal_back()
            review.score_current_card(Flashcard.Score.AUTO)
        assert review._autoscore_task is not None
        await review._autoscore_task
        for c in review._cards:
            assert c.state == Flashcard.State.SCORED
            assert c.score == Flashcard.Score.GOOD
        assert review.state == FlashcardReviewViewModel.State.DONE
        assert review._auto_scorer.invocations == 1

    async def test_batch_again_requeues_to_next_round(self, review):
        review._auto_scorer = _FakeScorer({1: 3, 2: 1, 3: 3})
        review.begin()
        for _ in range(3):
            review.current_card.reveal_back()
            review.score_current_card(Flashcard.Score.AUTO)
        await review._autoscore_task
        # Card 2 moved to end, added to (new) remaining
        assert [c.id for c in review._cards] == [1, 3, 2]
        assert 2 in review._remaining_before_batched_autoscore
        assert review._cards[2].state == Flashcard.State.AWAITING_REVEAL
        # Not done — user must re-rate card 2
        assert review.state == FlashcardReviewViewModel.State.REVIEWING

    async def test_manual_override_on_pending_auto(self, review):
        """Before the batch runs, the user can pre-empt a deferred card
        with a manual rating."""
        review._auto_scorer = _FakeScorer({1: 3, 2: 3})
        review.begin()
        # Defer card 1
        review.current_card.reveal_back()
        review.score_current_card(Flashcard.Score.AUTO)
        # Cursor now at card 2 (FRONT). Back up to card 1 and override.
        review.prev_card()
        assert review.current_card.id == 1
        assert review.current_card.state == Flashcard.State.REVEALED_PENDING_AUTO_SCORE
        review.score_current_card(Flashcard.Score.GOOD)
        assert review._cards[0].state == Flashcard.State.SCORED
        assert review._cards[0].score == Flashcard.Score.GOOD

    async def test_batch_double_spawn_is_prevented(self, review):
        """If the user drains the round during the batch, _check_remaining_cards
        must NOT spawn a second batch concurrently."""
        # Scorer that stalls until we release it
        release = asyncio.Event()
        original_ainvoke_results = {1: 3, 2: 3, 3: 3}

        class _StallingScorer:
            def __init__(self):
                self.structured_response = None
                self.invocations = 0

            async def ainvoke(self, prompt):
                self.invocations += 1
                await release.wait()
                self.structured_response = SimpleNamespace(
                    results=[
                        SimpleNamespace(flashcard_id=i, score=s, feedback="")
                        for i, s in original_ainvoke_results.items()
                    ]
                )

        scorer = _StallingScorer()
        review._auto_scorer = scorer
        review.begin()
        for _ in range(3):
            review.current_card.reveal_back()
            review.score_current_card(Flashcard.Score.AUTO)
        assert review.autoscore_in_progress
        # Yield so the batch task actually enters ainvoke
        await asyncio.sleep(0)
        assert scorer.invocations == 1
        # Manually poke _check_remaining_cards — the guard must prevent a
        # second task from spawning while the first is in flight
        review._check_remaining_cards()
        await asyncio.sleep(0)
        assert scorer.invocations == 1  # still only one batch spawned
        release.set()
        await review._autoscore_task
        assert scorer.invocations == 1  # confirmed: only one invocation

    async def test_scorer_exception_falls_back_to_manual(self, review):
        """When the scorer raises, pending-auto cards should fall back to
        manual rating (REVEALED_NOT_SCORED, back in _remaining,
        auto_scoring_failed=True) rather than looping the batch forever."""
        class _BrokenScorer:
            def __init__(self):
                self.structured_response = None

            async def ainvoke(self, prompt):
                raise RuntimeError("scorer is broken")

        review._auto_scorer = _BrokenScorer()
        review.begin()
        for _ in range(3):
            review.current_card.reveal_back()
            review.score_current_card(Flashcard.Score.AUTO)
        await review._autoscore_task
        assert review._remaining_before_batched_autoscore == {1, 2, 3}
        for c in review._cards:
            assert c.state == Flashcard.State.REVEALED_NOT_SCORED
            assert c.score is None
            assert c.auto_scoring_failed
        # Session continues so user can rate manually.
        assert review.state == FlashcardReviewViewModel.State.REVIEWING

    async def test_per_card_scorer_failure_reverts_only_that_card(self, review):
        """If the scorer drops a single card (no result for that id), that
        card is reverted to REVEALED_NOT_SCORED; the rest score normally."""
        # Scorer only returns results for cards 1 and 3 — card 2 dropped.
        review._auto_scorer = _FakeScorer({1: 3, 3: 3})
        review.begin()
        for _ in range(3):
            review.current_card.reveal_back()
            review.score_current_card(Flashcard.Score.AUTO)
        await review._autoscore_task

        card1, card2, card3 = review._cards[0], review._cards[1], review._cards[2]
        assert card1.state == Flashcard.State.SCORED
        assert card1.score == Flashcard.Score.GOOD
        assert not card1.auto_scoring_failed

        assert card2.state == Flashcard.State.REVEALED_NOT_SCORED
        assert card2.score is None
        assert card2.auto_scoring_failed

        assert card3.state == Flashcard.State.SCORED
        assert card3.score == Flashcard.Score.GOOD

        assert review._remaining_before_batched_autoscore == {2}
        assert review.state == FlashcardReviewViewModel.State.REVIEWING

    async def test_out_of_range_score_reverts_card(self, review):
        """A scorer returning 7 for a card should revert it, not try to
        apply an invalid rating."""
        review._auto_scorer = _FakeScorer({1: 3, 2: 7, 3: 3})
        review.begin()
        for _ in range(3):
            review.current_card.reveal_back()
            review.score_current_card(Flashcard.Score.AUTO)
        await review._autoscore_task

        card2 = review._cards[1]
        assert card2.auto_scoring_failed
        assert card2.state == Flashcard.State.REVEALED_NOT_SCORED
        assert 2 in review._remaining_before_batched_autoscore

    async def test_enter_on_failed_card_falls_back_to_good(self, review):
        """After a card's auto-scoring fails, pressing enter on its REVEALED
        state should rate it GOOD rather than re-deferring to auto."""
        # Single-card scenario: card 1 fails, cards 2/3 score OK
        review._auto_scorer = _FakeScorer({2: 3, 3: 3})  # card 1 missing
        review.begin()
        for _ in range(3):
            review.current_card.reveal_back()
            review.score_current_card(Flashcard.Score.AUTO)
        await review._autoscore_task

        # Card 1 is now the failed one — navigate to it.
        while review.current_card.id != 1:
            review.next_card()
        assert review.current_card.auto_scoring_failed
        assert review.current_card.state == Flashcard.State.REVEALED_NOT_SCORED

        # Simulate the user pressing enter (via the key handler). Must rate
        # GOOD, not re-defer to AUTO (which would assert).
        key = SimpleNamespace(key="enter")
        review._on_key_reviewing(key)

        assert review.current_card.state == Flashcard.State.SCORED
        assert review.current_card.score == Flashcard.Score.GOOD
        assert review.state == FlashcardReviewViewModel.State.DONE


# ---------------------------------------------------------------------------
# Regression / integration
# ---------------------------------------------------------------------------


class TestRegressions:
    """Narrow regression tests for bugs surfaced during design."""

    async def test_scoring_auto_then_nav_does_not_raise(self, review):
        """Reported earlier: navigating away from a SCORED card crashed
        because ``deactivate()`` tried to pause an already-stopped timer.
        With the FRONT-only guard, this should be safe."""
        review._auto_scorer = _FakeScorer({1: 3})
        review.begin()
        review.current_card.reveal_back()
        review.score_current_card(Flashcard.Score.AUTO)
        # Cursor on card 2 (FRONT, timer running). Navigate to/past the
        # PENDING_AUTO card 1. Must not raise.
        review.prev_card()  # to card 1 (PENDING_AUTO)
        review.next_card()  # back to card 2 (FRONT)
        assert review.current_card.id == 2

    async def test_awaiting_reveal_nav_does_not_raise(self, review):
        """Reported earlier: navigating to an AWAITING_REVEAL card crashed
        because ``activate()`` tried to start its main timer — and then
        ``reveal_front`` tried to start it again. With the FRONT-only guard,
        nav leaves the timer alone; reveal_front is the sole starter."""
        review.begin()
        # Rate card 1 AGAIN so it ends up in AWAITING_REVEAL at the end of
        # the list. After the requeue the cursor lands on card 2.
        review.current_card.reveal_back()
        review.score_current_card(Flashcard.Score.AGAIN)
        # prev_card from index 0 wraps to index 2 → card 1 (AWAITING_REVEAL)
        review.prev_card()
        assert review.current_card.id == 1
        assert review.current_card.state == Flashcard.State.AWAITING_REVEAL
        # Now user presses enter (reveal_front) — must not raise
        review.current_card.reveal_front()
        assert review.current_card.state == Flashcard.State.FRONT
        assert review.current_card._timer.running

    async def test_score_again_card_via_reveal_front_drains_next_remaining(self, review):
        """Regression: ``score_current_card`` only discarded from
        ``_remaining``, not from ``_next_remaining``. AGAIN'ing card 1,
        then immediately surfacing it via ``reveal_front`` and rating it
        normally, left a ghost id in ``_next``. After the round-swap the
        ghost landed in ``_remaining`` and the session never finished."""
        review.begin()
        # AGAIN card 1 → list [2, 3, 1], _next = {1}, card 1 AWAITING
        review.current_card.reveal_back()
        review.score_current_card(Flashcard.Score.AGAIN)
        assert 1 in review._next_remaining_before_batched_autoscore

        # Navigate to card 1 and surface it manually (impatient user)
        review.next_card()  # → card 3
        review.next_card()  # → card 1
        assert review.current_card.id == 1
        review.current_card.reveal_front()
        review.current_card.reveal_back()
        review.score_current_card(Flashcard.Score.GOOD)

        # The fix: card 1's id is removed from _next on score
        assert 1 not in review._next_remaining_before_batched_autoscore

        # Score the rest — session should reach DONE
        while review.current_card.id != 2:
            review.next_card()
        review.current_card.reveal_back()
        review.score_current_card(Flashcard.Score.GOOD)

        while review.current_card.id != 3:
            review.next_card()
        review.current_card.reveal_back()
        review.score_current_card(Flashcard.Score.GOOD)

        assert review.state == FlashcardReviewViewModel.State.DONE

    async def test_again_then_auto_others_drains_with_no_ghost(self, review):
        """Same ghost-id bug as above, but exercised through the auto-score
        path. AGAIN card 1, score it manually before its due-timer; AUTO
        card 2; GOOD card 3. Without the fix, the post-batch ``_check``
        finds ``_remaining = {1}`` after swap (ghost) and never finishes."""
        review._auto_scorer = _FakeScorer({2: 3})
        review.begin()
        review.current_card.reveal_back()
        review.score_current_card(Flashcard.Score.AGAIN)

        # Surface and score card 1 manually
        review.next_card()
        review.next_card()
        assert review.current_card.id == 1
        review.current_card.reveal_front()
        review.current_card.reveal_back()
        review.score_current_card(Flashcard.Score.GOOD)

        # Score card 2 AUTO
        while review.current_card.id != 2:
            review.next_card()
        review.current_card.reveal_back()
        review.score_current_card(Flashcard.Score.AUTO)

        # Score card 3 GOOD → drains _remaining → batch fires for card 2
        while review.current_card.id != 3:
            review.next_card()
        review.current_card.reveal_back()
        review.score_current_card(Flashcard.Score.GOOD)

        assert review._autoscore_task is not None
        await review._autoscore_task
        assert review.state == FlashcardReviewViewModel.State.DONE

    async def test_alt_s_skip_drains_remaining_triggers_batch(self, review):
        """Regression: alt+s skip didn't call ``_check_remaining_cards``.
        If skipping drained ``_remaining`` while a PENDING_AUTO card was
        deferred, the batch never fired and the session sat inert until
        the user manually overrode the deferred card."""
        review._auto_scorer = _FakeScorer({1: 3})
        review.begin()
        # Defer card 1 to AUTO
        review.current_card.reveal_back()
        review.score_current_card(Flashcard.Score.AUTO)
        # Cursor on card 2 (FRONT)
        assert review.current_card.id == 2

        # alt+s skip card 2
        key = SimpleNamespace(key="alt+s")
        review._on_key_reviewing(key)
        assert review._cards[1].score == Flashcard.Score.SKIPPED

        # Navigate to card 3 and alt+s skip
        while review.current_card.id != 3:
            review.next_card()
        review._on_key_reviewing(key)
        assert review._cards[2].score == Flashcard.Score.SKIPPED

        # The fix: alt+s now calls _check_remaining_cards, which finds
        # _remaining drained with PENDING_AUTO present → spawns the batch.
        assert review._autoscore_task is not None
        await review._autoscore_task
        assert review.state == FlashcardReviewViewModel.State.DONE

    async def test_all_again_swaps_queues_cleanly(self, review):
        """Invariant: AGAIN'ing every card in the round must drain
        ``_remaining`` and (after the round-swap triggered by
        ``_check_remaining_cards``) leave ``_next_remaining`` empty and
        ``_remaining`` holding all the cards. No ghost ids, no stalled
        session in REVIEWING."""
        review.begin()
        for _ in range(3):
            review.current_card.reveal_back()
            review.score_current_card(Flashcard.Score.AGAIN)
        assert review._remaining_before_batched_autoscore == {1, 2, 3}
        assert review._next_remaining_before_batched_autoscore == set()
        assert review.state == FlashcardReviewViewModel.State.REVIEWING

    async def test_drain_to_auto_during_in_flight_batch_spawns_second_batch(self, review):
        """Regression-as-invariant: scoring AUTO on the only remaining
        card while an earlier batch is still in flight must result in a
        second batch being dispatched once the first completes — not a
        stalled session. Relies on ``_handle_batched_auto_score`` calling
        ``_check_remaining_cards`` in its tail, which recursively spawns
        the next batch when there's still pending-AUTO work."""

        class _MultiBatchScorer:
            def __init__(self):
                self.structured_response = None
                self.invocations = 0
                self.first_batch_release = asyncio.Event()

            async def ainvoke(self, prompt):
                self.invocations += 1
                if self.invocations == 1:
                    await self.first_batch_release.wait()
                    results = {2: 3}  # card 2 → GOOD
                else:
                    results = {1: 3}  # card 1 → GOOD
                self.structured_response = SimpleNamespace(
                    results=[
                        SimpleNamespace(flashcard_id=fc_id, score=s, feedback="")
                        for fc_id, s in results.items()
                    ]
                )

        scorer = _MultiBatchScorer()
        review._auto_scorer = scorer
        review.begin()

        # AGAIN card 1 → list [2, 3, 1], _next = {1}, cursor on card 2
        review.current_card.reveal_back()
        review.score_current_card(Flashcard.Score.AGAIN)

        # AUTO card 2 → PENDING_AUTO, cursor on card 3
        review.current_card.reveal_back()
        review.score_current_card(Flashcard.Score.AUTO)

        # GOOD card 3 → drains _remaining → swap _next → _remaining = {1};
        # pending = [card 2] → spawn first batch. Cursor lands on card 1
        # (AWAITING_REVEAL, the only thing in _next at goto-time).
        review.current_card.reveal_back()
        review.score_current_card(Flashcard.Score.GOOD)

        # Yield so the batch task actually enters ainvoke
        await asyncio.sleep(0)
        assert scorer.invocations == 1
        assert review.autoscore_in_progress

        # Surface card 1 manually and AUTO it while the first batch is stalled
        assert review.current_card.id == 1
        review.current_card.reveal_front()
        review.current_card.reveal_back()
        review.score_current_card(Flashcard.Score.AUTO)
        # Card 1 PENDING_AUTO; _remaining = {}, _next = {}; first batch
        # still in flight (autoscore_in_progress guards against re-spawn).
        assert review.autoscore_in_progress
        first_task = review._autoscore_task

        # Release the first batch. Its tail _check_remaining_cards finds
        # _remaining drained, no in-flight batch (just cleared), and
        # pending = [card 1] → spawns the second batch. The second batch's
        # ainvoke has no awaits to stall on, so it may fully complete
        # during the `await first_task` below — that's fine, the invariant
        # is that the second invocation happened at all.
        scorer.first_batch_release.set()
        await first_task

        # Drain any still-pending second-batch work
        if review._autoscore_task is not None and not review._autoscore_task.done():
            await review._autoscore_task

        assert scorer.invocations == 2
        assert review.state == FlashcardReviewViewModel.State.DONE
