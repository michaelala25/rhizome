"""Key → FlashcardReviewAction binding layer for ``FlashcardReview``.

This module is the *only* place that knows about keyboard bindings. It is
view-agnostic — it imports nothing from Textual — and read-only with respect to
the view-model: ``key_to_action`` consults VM state to disambiguate context-
sensitive bindings (e.g. all five "enter" flavors in REVIEWING) but never
mutates anything and never emits.

Layering:

    View  ──┐                          ┌──> VM.dispatch(action)
            │  on_key(event):          │
            │    action =              │
            │      key_to_action(      │  VM owns the FlashcardReviewAction
            │        event.key, vm)    │  enum and exposes pure action methods
            │    if action: vm.dispatch(action)  (no Textual import, no event
            └──> keymap.key_to_action ─┘  handling).

The ``FlashcardReviewAction`` enum lives on the view-model — it's the VM's
action vocabulary, and the keymap is a consumer. ``KEYBINDINGS`` here provides
the display-friendly key string for each action; the same table doubles as a
label-source for a future contextual help dropdown.
"""

from rhizome.app.flashcard_review.flashcard import Flashcard
from rhizome.app.flashcard_review.review import (
    FlashcardReviewAction,
    FlashcardReviewVM,
)


KEYBINDINGS: dict[FlashcardReviewAction, str] = {
    FlashcardReviewAction.BEGIN: "enter",
    FlashcardReviewAction.CANCEL: "ctrl+c",
    FlashcardReviewAction.PREV_CARD: "alt+left",
    FlashcardReviewAction.NEXT_CARD: "alt+right",
    FlashcardReviewAction.REVEAL_BACK: "enter",
    FlashcardReviewAction.REVEAL_FRONT: "enter",
    FlashcardReviewAction.SCORE_DEFAULT: "enter",
    FlashcardReviewAction.SCORE_DEFAULT_GOOD: "enter",
    FlashcardReviewAction.ADVANCE_NEXT: "enter",
    FlashcardReviewAction.APPROVE_AUTO_SCORE: "enter",
    FlashcardReviewAction.SCORE_AGAIN: "1",
    FlashcardReviewAction.SCORE_HARD: "2",
    FlashcardReviewAction.SCORE_GOOD: "3",
    FlashcardReviewAction.SCORE_EASY: "4",
    FlashcardReviewAction.REJECT_AUTO_SCORE: "d",
    FlashcardReviewAction.TOGGLE_TIMER: "ctrl+k",
    FlashcardReviewAction.RESET_CARD: "alt+x",
    FlashcardReviewAction.TOGGLE_SKIP: "alt+s",
    FlashcardReviewAction.TOGGLE_FLAG: "alt+m",
    FlashcardReviewAction.TOGGLE_AUTO_APPROVE_AUTO_SCORE: "shift+tab",
    FlashcardReviewAction.TOGGLE_COLLAPSED: "enter",
    FlashcardReviewAction.TOGGLE_HELP: "alt+h",
    FlashcardReviewAction.TOGGLE_AUTO_SCORE: "alt+a",
    FlashcardReviewAction.ACCEPT_ALL_AUTO_SCORES: "ctrl+enter",
}


# Textual reports certain Ctrl+<letter> keypresses as their underlying control-
# character names — most notably ``ctrl+enter`` arrives as ``ctrl+j``. Keep
# KEYBINDINGS using the user-friendly form (so the UI displays "ctrl+enter") and
# resolve aliases here when matching against ``event.key``.
_KEY_ALIASES: dict[str, str] = {
    "ctrl+j": "ctrl+enter",
}


def _matches_binding(event_key: str, binding: str) -> bool:
    """True iff ``event_key`` matches ``binding`` directly or via a known
    control-char alias (see ``_KEY_ALIASES``)."""
    return event_key == binding or _KEY_ALIASES.get(event_key) == binding


_SCORE_KEY_TO_ACTION: dict[str, FlashcardReviewAction] = {
    KEYBINDINGS[FlashcardReviewAction.SCORE_AGAIN]: FlashcardReviewAction.SCORE_AGAIN,
    KEYBINDINGS[FlashcardReviewAction.SCORE_HARD]: FlashcardReviewAction.SCORE_HARD,
    KEYBINDINGS[FlashcardReviewAction.SCORE_GOOD]: FlashcardReviewAction.SCORE_GOOD,
    KEYBINDINGS[FlashcardReviewAction.SCORE_EASY]: FlashcardReviewAction.SCORE_EASY,
}


def key_to_action(key: str, vm: FlashcardReviewVM) -> FlashcardReviewAction | None:
    """Resolve a key press to a semantic FlashcardReviewAction given the current VM state.

    Returns ``None`` if the key has no meaning in the current context (the View
    should treat that as "not handled" and let the event propagate).
    """
    S = FlashcardReviewVM.State

    # Cross-state — checked first so they win regardless of state.
    if key == KEYBINDINGS[FlashcardReviewAction.TOGGLE_HELP]:
        return FlashcardReviewAction.TOGGLE_HELP
    if key == KEYBINDINGS[FlashcardReviewAction.TOGGLE_AUTO_SCORE]:
        # toggle_auto_score_enabled asserts state == REVIEWING; only emit the
        # action when we're actually in REVIEWING.
        if vm.state == S.REVIEWING:
            return FlashcardReviewAction.TOGGLE_AUTO_SCORE
        return None

    if vm.state == S.START:
        return _start_action(key)
    if vm.state == S.REVIEWING:
        return _reviewing_action(key, vm)
    if vm.state == S.DONE:
        return _done_action(key)
    return None


def _start_action(key: str) -> FlashcardReviewAction | None:
    if key == KEYBINDINGS[FlashcardReviewAction.BEGIN]:
        return FlashcardReviewAction.BEGIN
    if key == KEYBINDINGS[FlashcardReviewAction.CANCEL]:
        return FlashcardReviewAction.CANCEL
    return None


def _reviewing_action(key: str, vm) -> FlashcardReviewAction | None:
    # Trivial 1:1 bindings.
    simple: dict[str, FlashcardReviewAction] = {
        KEYBINDINGS[FlashcardReviewAction.PREV_CARD]: FlashcardReviewAction.PREV_CARD,
        KEYBINDINGS[FlashcardReviewAction.NEXT_CARD]: FlashcardReviewAction.NEXT_CARD,
        KEYBINDINGS[FlashcardReviewAction.CANCEL]: FlashcardReviewAction.CANCEL,
        KEYBINDINGS[FlashcardReviewAction.TOGGLE_TIMER]: FlashcardReviewAction.TOGGLE_TIMER,
        KEYBINDINGS[FlashcardReviewAction.TOGGLE_FLAG]: FlashcardReviewAction.TOGGLE_FLAG,
        KEYBINDINGS[FlashcardReviewAction.TOGGLE_AUTO_APPROVE_AUTO_SCORE]: FlashcardReviewAction.TOGGLE_AUTO_APPROVE_AUTO_SCORE,
        KEYBINDINGS[FlashcardReviewAction.RESET_CARD]: FlashcardReviewAction.RESET_CARD,
        KEYBINDINGS[FlashcardReviewAction.TOGGLE_SKIP]: FlashcardReviewAction.TOGGLE_SKIP,
    }
    if key in simple:
        return simple[key]
    if _matches_binding(key, KEYBINDINGS[FlashcardReviewAction.ACCEPT_ALL_AUTO_SCORES]):
        return FlashcardReviewAction.ACCEPT_ALL_AUTO_SCORES

    card = vm.current_card

    # Digits 1-4: only meaningful on revealed / pending-approval cards.
    if key in _SCORE_KEY_TO_ACTION and card is not None and card.state in (
        Flashcard.State.REVEALED_NOT_SCORED,
        Flashcard.State.REVEALED_PENDING_AUTO_SCORE,
        Flashcard.State.SCORED_PENDING_APPROVAL,
    ):
        return _SCORE_KEY_TO_ACTION[key]

    # 'd' rejects a staged auto-score (only when one is staged).
    if (
        key == KEYBINDINGS[FlashcardReviewAction.REJECT_AUTO_SCORE]
        and card is not None
        and card.state == Flashcard.State.SCORED_PENDING_APPROVAL
    ):
        return FlashcardReviewAction.REJECT_AUTO_SCORE

    # The five-way "enter" disambiguation. All five flavors share the same
    # binding string; the meaning is determined by the current card's state and
    # (in the REVEALED_NOT_SCORED case) the auto-score config + per-card
    # failure / discard latches.
    if key == KEYBINDINGS[FlashcardReviewAction.REVEAL_BACK] and card is not None:
        match card.state:
            case Flashcard.State.FRONT:
                return FlashcardReviewAction.REVEAL_BACK
            case Flashcard.State.AWAITING_REVEAL:
                return FlashcardReviewAction.REVEAL_FRONT
            case Flashcard.State.REVEALED_NOT_SCORED:
                if (
                    vm.auto_score_enabled
                    and not card.auto_scoring_failed
                    and not card.auto_score_discarded
                ):
                    return FlashcardReviewAction.SCORE_DEFAULT
                return FlashcardReviewAction.SCORE_DEFAULT_GOOD
            case Flashcard.State.SCORED_PENDING_APPROVAL:
                return FlashcardReviewAction.APPROVE_AUTO_SCORE
            case Flashcard.State.SCORED | Flashcard.State.REVEALED_PENDING_AUTO_SCORE:
                return FlashcardReviewAction.ADVANCE_NEXT
    return None


def _done_action(key: str) -> FlashcardReviewAction | None:
    if key == KEYBINDINGS[FlashcardReviewAction.PREV_CARD]:
        return FlashcardReviewAction.PREV_CARD
    if key == KEYBINDINGS[FlashcardReviewAction.NEXT_CARD]:
        return FlashcardReviewAction.NEXT_CARD
    if key == KEYBINDINGS[FlashcardReviewAction.TOGGLE_COLLAPSED]:
        return FlashcardReviewAction.TOGGLE_COLLAPSED
    return None
