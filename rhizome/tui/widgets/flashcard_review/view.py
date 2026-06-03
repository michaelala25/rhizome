"""FlashcardReview — thin Textual view over FlashcardReviewVM."""

from __future__ import annotations

from typing import Any

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Rule, Static, TextArea

from fsrs import Rating

from rhizome.tui.widgets.flashcard_review.dot_strips import _DotStrip
from rhizome.app.flashcard_review.review import (
    Flashcard,
    FlashcardData,
    FlashcardReviewVM,
)
from rhizome.tui.widgets.shared.navigable_feed_item import NavigableFeedItemViewBase


# Throbber frames — pulsing dot used in both the counter (think-time) and
# the batch-scoring indicator. Shared frame counter; cadence set per-interval.
_THROBBER_FRAMES: list[tuple[str, str]] = [
    ("•", "rgb(60,65,80)"),
    ("●", "rgb(60,65,80)"),
    ("●", "rgb(95,105,125)"),
    ("⬤", "rgb(130,140,160)"),
    ("●", "rgb(95,105,125)"),
    ("●", "rgb(60,65,80)"),
]

# Per-rating colors — the rating digits get a red→green gradient matching
# the severity of the rating. Labels stay dim so the digits read first.
_RATING_COLORS: dict[int, str] = {
    1: "rgb(235,100,100)",  # again — red
    2: "rgb(230,160,80)",   # hard  — orange
    3: "rgb(200,220,100)",  # good  — yellow-green
    4: "rgb(120,210,110)",  # easy  — green
}
_RATING_LABEL_DIM = "rgb(110,110,110)"
_HINT_DIM = "rgb(80,80,80)"
_FAIL_RED = "rgb(235,100,100)"
_DONE_GREEN = "rgb(120,210,110)"
_CANCEL_RED = "rgb(235,100,100)"

# Yellowish-orange used to call attention to "user action awaited on an auto-score":
# the dot for any SCORED_PENDING_APPROVAL card, and the [enter] approve hint.
_APPROVAL_YELLOW = "rgb(235,180,90)"

# How long messages remain visible before being wiped
_MESSAGE_DISPLAY_SECONDS = 3.0


def _format_due(seconds: float) -> str:
    """Anki-style compact interval label for a rating preview.

    Picks the largest unit that yields a value >= 1 (rounded), so 9000s
    renders as ``2h`` rather than ``150m``. Sub-minute durations (the
    Learning step ladder) all show as ``<1m`` — finer granularity isn't
    useful when the card is going to be requeued in-widget anyway.

    Day-or-larger intervals are prefixed with ``~`` because FSRS applies
    a small randomized fuzz factor to those values (the actual due date
    when the user picks the rating may differ from the preview by a
    handful of percent). Sub-day intervals aren't fuzzed.
    """
    if seconds < 60:
        return "<1m"
    minutes = seconds / 60
    if minutes < 60:
        return f"{round(minutes)}m"
    hours = minutes / 60
    if hours < 24:
        return f"{round(hours)}h"
    days = hours / 24
    if days < 30:
        return f"~{round(days)}d"
    months = days / 30
    if months < 12:
        return f"~{round(months)}mo"
    return f"~{round(days / 365)}y"


class _AnswerInput(TextArea):
    """TextArea that forwards Enter up to the parent instead of inserting a newline."""

    class Submitted(Message):
        pass

    def __init__(self, **kwargs) -> None:
        super().__init__(show_line_numbers=False, **kwargs)

    def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted())
            return
        # Let alt+<whatever> bubble up to the parent so app-level bindings
        # (alt+r reset, alt+s skip, alt+t timer, alt+h help, alt+left/right nav) don't
        # get swallowed as literal text input.
        if event.key.startswith("alt+"):
            event.prevent_default()


class FlashcardReview(NavigableFeedItemViewBase[FlashcardReviewVM]):

    # Key → action map. Each binding is gated by ``check_action`` against VM state, so several
    # bindings can share a key and Textual falls through to the first whose guard passes. "enter"
    # is the prime case: one flavor per context (begin / reveal / score-default / approve / advance /
    # expand). All "enter" flavors share the ``confirm`` id so a keymap rebinds the confirm key
    # everywhere at once. Bindings carry ids (for a future user keymap) but ``show=False`` — the
    # widget renders its own on-screen prompts and help list rather than a Footer.
    BINDINGS = [
        # Cross-state
        Binding("alt+h", "toggle_help", "Toggle help", id="flashcard_review.toggle_help", show=False),

        # START
        Binding("enter", "begin", "Begin", id="flashcard_review.confirm", show=False),

        # REVIEWING — contextual "enter" flavors (one card-state each; gated in check_action)
        Binding("enter", "reveal_back", "Reveal answer", id="flashcard_review.confirm", show=False),
        Binding("enter", "reveal_front", "Hide answer", id="flashcard_review.confirm", show=False),
        Binding("enter", "score_default", "Score (auto)", id="flashcard_review.confirm", show=False),
        Binding("enter", "score_default_good", "Score good", id="flashcard_review.confirm", show=False),
        Binding("enter", "approve_auto_score", "Approve auto-score", id="flashcard_review.confirm", show=False),
        Binding("enter", "advance_next", "Next card", id="flashcard_review.confirm", show=False),

        # REVIEWING — manual ratings + auto-score reject
        Binding("1", "score_again", "Score again", id="flashcard_review.score_again", show=False),
        Binding("2", "score_hard", "Score hard", id="flashcard_review.score_hard", show=False),
        Binding("3", "score_good", "Score good", id="flashcard_review.score_good", show=False),
        Binding("4", "score_easy", "Score easy", id="flashcard_review.score_easy", show=False),
        Binding("d", "reject_auto_score", "Reject auto-score", id="flashcard_review.reject_auto_score", show=False),

        # REVIEWING / DONE — navigation + lifecycle
        Binding("alt+left", "prev_card", "Previous card", id="flashcard_review.prev_card", show=False),
        Binding("alt+right", "next_card", "Next card", id="flashcard_review.next_card", show=False),
        Binding("ctrl+c", "cancel", "Cancel session", id="flashcard_review.cancel", show=False),

        # REVIEWING — toggles / card ops
        # alt+ rather than ctrl+ on purpose: while a FRONT card is shown the answer-input TextArea is
        # focused, and it binds several ctrl+ keys (e.g. ctrl+k = delete-to-end-of-line). The focused
        # input sits ahead of this widget in the binding chain, so a ctrl+ key it owns would win over
        # ours. TextArea binds no alt+ editing keys, so alt+ combos reach us cleanly.
        Binding("alt+t", "toggle_timer", "Toggle timer", id="flashcard_review.toggle_timer", show=False),
        Binding("alt+m", "toggle_flag", "Flag / unflag", id="flashcard_review.toggle_flag", show=False),
        Binding("alt+r", "reset_card", "Reset card", id="flashcard_review.reset_card", show=False),
        Binding("alt+s", "toggle_skip", "Skip / unskip", id="flashcard_review.toggle_skip", show=False),
        Binding("shift+tab", "toggle_auto_approve", "Toggle auto-approve", id="flashcard_review.toggle_auto_approve", show=False),
        Binding("alt+a", "toggle_auto_score", "Toggle auto-score default", id="flashcard_review.toggle_auto_score", show=False),
        # ctrl+enter arrives as ctrl+j on most terminals — bind the byte we actually receive and
        # show the friendly form in the help text.
        Binding("ctrl+j", "accept_all_auto_scores", "Approve all", id="flashcard_review.accept_all", show=False, key_display="ctrl+enter"),

        # DONE
        Binding("enter", "toggle_collapsed", "Expand / collapse", id="flashcard_review.confirm", show=False),
    ]

    DEFAULT_CSS = """
    FlashcardReview {
        height: auto;
        layout: vertical;
        padding: 0 1;
    }
    FlashcardReview #fr-collapse {
        dock: right;
        width: auto;
        min-width: 3;
        height: 1;
        background: transparent;
        border: none;
        color: rgb(100,100,100);
        display: none;
    }
    FlashcardReview #fr-collapse:hover {
        color: rgb(200,200,200);
    }
    FlashcardReview #fr-batch-indicator {
        dock: top;
        height: 1;
        text-align: right;
        color: rgb(150,160,200);
        padding: 0 2;
    }
    FlashcardReview #fr-start,
    FlashcardReview #fr-card,
    FlashcardReview #fr-done {
        border: solid rgb(58,65,80);
        padding: 1 2;
        margin: 0 4;
        height: auto;
    }
    FlashcardReview #fr-start,
    FlashcardReview #fr-done {
        align: center middle;
    }
    FlashcardReview .fr-label {
        text-style: bold;
        color: rgb(100,100,100);
    }
    FlashcardReview #fr-header {
        height: 1;
        margin: 0 0 1 0;
    }
    FlashcardReview #fr-question-label {
        width: auto;
    }
    FlashcardReview #fr-counter {
        width: 1fr;
        text-align: right;
        color: rgb(100,100,100);
    }
    FlashcardReview #fr-question {
        color: rgb(195,195,205);
        margin: 0 0 1 0;
    }
    FlashcardReview #fr-answer-input {
        height: auto;
        min-height: 1;
        max-height: 5;
        border: solid rgb(35,38,48);
        background: transparent;
    }
    FlashcardReview #fr-answer-input:focus {
        border: solid rgb(55,60,72);
    }
    FlashcardReview #fr-user-answer {
        color: rgb(170,175,190);
        margin: 0;
    }
    FlashcardReview #fr-ua-rule {
        color: rgb(58,65,80);
        margin: 0 0 0 0;
    }
    FlashcardReview #fr-answer {
        color: rgb(210,200,175);
    }
    FlashcardReview #fr-below {
        text-align: center;
        margin: 1 0 0 0;
    }
    FlashcardReview #fr-dots-row {
        height: 1;
        margin: 1 0 0 0;
    }
    FlashcardReview #fr-dots-row > Static,
    FlashcardReview #fr-dots-row > _DotStrip {
        width: 1fr;
        height: 1;
    }
    FlashcardReview #fr-dots {
        text-align: center;
    }
    FlashcardReview #fr-help-hint {
        text-align: left;
        color: rgb(80,80,80);
    }
    FlashcardReview #fr-bottom-row {
        height: 1;
        margin: 0;
    }
    FlashcardReview #fr-bottom-row > Static {
        width: 1fr;
        height: 1;
    }
    FlashcardReview #fr-auto-approve-hint {
        text-align: left;
    }
    FlashcardReview #fr-message {
        text-align: right;
        color: rgb(80,80,80);
    }
    FlashcardReview #fr-start-summary,
    FlashcardReview #fr-start-prompt {
        text-align: center;
        width: 1fr;
        color: rgb(80,80,80);
    }
    FlashcardReview #fr-start-rule {
        color: rgb(58,65,80);
    }
    FlashcardReview #fr-done-status {
        text-align: center;
        width: 1fr;
    }
    FlashcardReview #fr-help {
        text-align: center;
        margin: 1 0 0 0;
        color: rgb(80,80,80);
        height: auto;
    }
    """

    def __init__(
        self,
        vm: FlashcardReviewVM,
        **kwargs,
    ) -> None:
        # ViewBase wires dirty→_refresh and focus→self.focus, and stores ``vm`` as ``self._vm``.
        super().__init__(vm, **kwargs)

        # Set while ``_refresh`` programmatically rewrites the TextArea's
        # contents; the Changed handler checks this to avoid echoing the
        # value right back into the card as a user edit.
        self._suppress_text_change = False

        # Interval handles, reconciled against VM state in _refresh_*.
        self._timer_interval = None    # think-time timer (FRONT + timer_visible)
        self._due_interval = None      # due countdown (AWAITING_REVEAL)
        self._throbber_interval = None # pulsing dot (FRONT w/o timer, or batch)

        # Shared throbber frame counter — advanced by _tick_throbber.
        self._throbber_frame = 0

        self._message_timer = None

    def compose(self) -> ComposeResult:
        yield Button("▼", id="fr-collapse")
        yield Static("", id="fr-batch-indicator")

        with Vertical(id="fr-start"):
            yield Static("", id="fr-start-summary")
            yield Rule(line_style="solid", id="fr-start-rule")
            yield Static(
                "Press [bold]enter[/bold] to begin.",
                id="fr-start-prompt",
            )

        with Vertical(id="fr-card"):
            
            with Horizontal(id="fr-header"):
                yield Static("", classes="fr-label", id="fr-question-label")
                yield Static("", id="fr-counter")

            yield Static("", id="fr-question")
            yield Static("Your answer", classes="fr-label", id="fr-answer-input-label")
            yield _AnswerInput(id="fr-answer-input")
            yield Static("Your answer", classes="fr-label", id="fr-user-answer-label")
            yield Static("", id="fr-user-answer")
            yield Rule(line_style="solid", id="fr-ua-rule")
            yield Static("Answer", classes="fr-label", id="fr-answer-label")
            yield Static("", id="fr-answer")
            yield Static("", id="fr-below")
            with Horizontal(id="fr-dots-row"):
                yield Static("", id="fr-help-hint")
                yield _DotStrip(id="fr-dots")
                yield Static("", id="fr-bottom-spacer")
            with Horizontal(id="fr-bottom-row"):
                yield Static("", id="fr-auto-approve-hint")
                yield Static("", id="fr-message")

        with Vertical(id="fr-done"):
            yield Static("", id="fr-done-status")

        yield Static("", id="fr-help")

    def on_mount(self) -> None:
        # Border-title nav hint on the card container — has to live in on_mount because it queries a
        # child widget.
        card_container = self.query_one("#fr-card", Vertical)
        card_container.border_title = "alt+←/→ to navigate"
        card_container.styles.border_title_align = "right"

        # Initial paint — also mount-deferred because _refresh queries children.
        self._refresh()

    def on_unmount(self) -> None:
        super().on_unmount()  # ViewBase tears down the dirty→_refresh / focus→self.focus subs

    def on_focus(self, event: events.Focus) -> None:
        """When the widget gains focus from outside (e.g. navigation via
        ctrl+up/down), route focus into the answer input if the current
        card is in FRONT. Without this, the focus-shift logic inside
        ``_refresh_current_card`` never runs for externally-triggered
        focus changes (no VM dirty emit is fired)."""
        super().on_focus(event)  # ViewBase → vm.notify_focused() → dirty → _refresh

        card = self._vm.current_card
        if card is None:
            return
        if (
            card.state == Flashcard.State.FRONT
            and self._vm.state == FlashcardReviewVM.State.REVIEWING
        ):
            self.query_one("#fr-answer-input", _AnswerInput).focus()

    def on__answer_input_submitted(
        self, event: _AnswerInput.Submitted
    ) -> None:
        # The answer input is only ever shown on a FRONT card, and the TextArea swallows enter
        # before the "enter" bindings can see it — so bridge that submit to the reveal directly.
        self._vm.reveal_back_current_card()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id != "fr-answer-input":
            return
        if self._suppress_text_change:
            return
        card = self._vm.current_card
        if card is None or card.state != Flashcard.State.FRONT:
            return
        card.set_user_answer(event.text_area.text.strip())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "fr-collapse":
            event.stop()
            self._vm.toggle_collapsed()
            self.focus()

    # ------------------------------------------------------------------
    # Bindings → VM actions
    #
    # ``check_action`` gates each binding against VM state; ``action_*`` forwards to a VM command.
    # For the contextual "enter" flavors it returns True for the single applicable flavor and False
    # for the rest, so Textual hides the inactive ones and falls through to the live one.
    # ------------------------------------------------------------------

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        vm = self._vm
        S = FlashcardReviewVM.State
        CS = Flashcard.State
        card = vm.current_card
        card_state = card.state if card is not None else None

        match action:
            case "toggle_help":
                return True

            case "begin":
                return vm.state == S.START
            case "cancel":
                return vm.state != S.DONE
            case "prev_card" | "next_card":
                return vm.state != S.START

            # Contextual "enter" flavors — one card-state each.
            case "reveal_back":
                return vm.state == S.REVIEWING and card_state == CS.FRONT
            case "reveal_front":
                return vm.state == S.REVIEWING and card_state == CS.AWAITING_REVEAL
            case "score_default":
                return (vm.state == S.REVIEWING and card_state == CS.REVEALED_NOT_SCORED
                        and vm.auto_score_active_for_current_card)
            case "score_default_good":
                return (vm.state == S.REVIEWING and card_state == CS.REVEALED_NOT_SCORED
                        and not vm.auto_score_active_for_current_card)
            case "approve_auto_score":
                return vm.state == S.REVIEWING and card_state == CS.SCORED_PENDING_APPROVAL
            case "advance_next":
                return vm.state == S.REVIEWING and card_state in (CS.SCORED, CS.REVEALED_PENDING_AUTO_SCORE)

            # Manual ratings apply to any revealed / pending card; reject only to a staged auto-score.
            case "score_again" | "score_hard" | "score_good" | "score_easy":
                return vm.state == S.REVIEWING and card_state in (
                    CS.REVEALED_NOT_SCORED, CS.REVEALED_PENDING_AUTO_SCORE, CS.SCORED_PENDING_APPROVAL,
                )
            case "reject_auto_score":
                return vm.state == S.REVIEWING and card_state == CS.SCORED_PENDING_APPROVAL

            case ("toggle_timer" | "toggle_flag" | "reset_card" | "toggle_skip"
                  | "toggle_auto_approve" | "toggle_auto_score" | "accept_all_auto_scores"):
                return vm.state == S.REVIEWING

            case "toggle_collapsed":
                return vm.state == S.DONE

        return True

    def action_toggle_help(self) -> None: self._vm.toggle_help_visible()
    def action_begin(self) -> None: self._vm.begin()
    def action_cancel(self) -> None: self._vm.cancel()
    def action_prev_card(self) -> None: self._vm.prev_card()
    def action_next_card(self) -> None: self._vm.next_card()
    def action_reveal_back(self) -> None: self._vm.reveal_back_current_card()
    def action_reveal_front(self) -> None: self._vm.reveal_front_current_card()
    def action_score_default(self) -> None: self._vm.score_current_card(Flashcard.Score.AUTO)
    def action_score_default_good(self) -> None: self._vm.score_current_card(Flashcard.Score.GOOD)
    def action_approve_auto_score(self) -> None: self._vm.approve_pending_score()
    def action_advance_next(self) -> None: self._vm.advance_to_next_unscored()
    def action_score_again(self) -> None: self._vm.score_current_card(Flashcard.Score.AGAIN)
    def action_score_hard(self) -> None: self._vm.score_current_card(Flashcard.Score.HARD)
    def action_score_good(self) -> None: self._vm.score_current_card(Flashcard.Score.GOOD)
    def action_score_easy(self) -> None: self._vm.score_current_card(Flashcard.Score.EASY)
    def action_reject_auto_score(self) -> None: self._vm.reject_pending_score()
    def action_toggle_timer(self) -> None: self._vm.toggle_timers_visible()
    def action_toggle_flag(self) -> None: self._vm.toggle_flag_current_card()
    def action_reset_card(self) -> None: self._vm.reset_current_card()
    def action_toggle_skip(self) -> None: self._vm.toggle_skip_current_card()
    def action_toggle_auto_approve(self) -> None: self._vm.toggle_auto_approve_auto_score()
    def action_toggle_auto_score(self) -> None: self._vm.toggle_auto_score_enabled()
    def action_accept_all_auto_scores(self) -> None: self._vm.accept_all_auto_scores()
    def action_toggle_collapsed(self) -> None: self._vm.toggle_collapsed()

    def _key_for(self, action: str) -> str:
        """Display key for an action, read from ``BINDINGS`` — the single source of truth so the
        on-screen hints stay in sync with what's actually bound (incl. the ctrl+enter key_display)."""
        for binding in self.BINDINGS:
            if binding.action == action:
                return binding.key_display or binding.key
        return "?"

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        match self._vm.state:
            case FlashcardReviewVM.State.START:
                self._refresh_start()
            case FlashcardReviewVM.State.REVIEWING:
                self._refresh_reviewing()
            case FlashcardReviewVM.State.DONE:
                self._refresh_done()
        self._refresh_help()
        self._refresh_auto_approve_hint()
        self._refresh_message()

    def _refresh_auto_approve_hint(self) -> None:
        """Status indicator under the bottom row: shows whether auto-scored ratings will
        be auto-applied (yellow) or staged for approval (dim), and the keybinding to
        toggle it. Visible only during REVIEWING — the toggle isn't wired in other
        states."""
        hint = self.query_one("#fr-auto-approve-hint", Static)
        if self._vm.state != FlashcardReviewVM.State.REVIEWING:
            hint.display = False
            return
        hint.display = True
        if self._vm.auto_approve_auto_score:
            status = f"[bold {_APPROVAL_YELLOW}]auto-approve enabled[/]"
        else:
            status = f"[{_HINT_DIM}]auto-approve disabled[/]"
        binding = self._key_for("toggle_auto_approve")
        hint.update(
            f"{status}  [{_HINT_DIM}]({binding} to toggle)[/]"
        )

    def _refresh_message(self) -> None:
        """Flash the VM's latest user-action message in the bottom-right slot.

        Pops the VM message so unrelated dirty emits don't re-display it after
        the timer has cleared the widget. A new pop restarts the 3s timer.
        """
        new_msg = self._vm.pop_latest_message()
        if new_msg is None:
            return
        if self._message_timer is not None:
            self._message_timer.stop()
        self.query_one("#fr-message", Static).update(new_msg)
        self._message_timer = self.set_timer(
            _MESSAGE_DISPLAY_SECONDS, self._clear_message,
        )

    def _clear_message(self) -> None:
        self.query_one("#fr-message", Static).update("")
        self._message_timer = None

    def _refresh_help(self) -> None:
        # Two slots:
        #   - #fr-help-hint sits inside the card (bottom-left), shown only
        #     when help is collapsed — a permanent reminder that alt+h
        #     opens the full list.
        #   - #fr-help sits outside the card and renders the full list
        #     when expanded.
        # Always-displayed (just blanked when expanded) so its 1fr column
        # stays in the layout — otherwise the dot strip would shift left.
        hint = self.query_one("#fr-help-hint", Static)
        if self._vm.help_visible:
            hint.update("")
        else:
            hint.update(f"[bold]{self._key_for('toggle_help')}[/]  show help")

        full = self.query_one("#fr-help", Static)
        full.display = self._vm.help_visible
        if self._vm.help_visible:
            full.update(self._help_text())

    def _help_text(self) -> str:
        # Only the non-obvious bindings — the on-screen rating row, dot
        # strip, and per-card prompts already cover begin/reveal/score/nav.
        current_default = "auto" if self._vm.auto_score_enabled else "good"
        auto_label = f"enter default = {current_default}"
        rows = [
            ("toggle_help", "hide help"),
            ("cancel", "cancel session"),
            ("toggle_timer", "toggle timer"),
            ("reset_card", "reset current card"),
            ("toggle_skip", "skip / unskip card"),
            ("toggle_flag", "flag / unflag card"),
            ("toggle_auto_score", auto_label),
        ]
        return "    ".join(
            f"[bold]{self._key_for(action)}[/]  {label}"
            for action, label in rows
        )

    def _refresh_start(self) -> None:
        self.query_one("#fr-start", Vertical).display = True
        self.query_one("#fr-card", Vertical).display = False
        self.query_one("#fr-done", Vertical).display = False
        self.query_one("#fr-batch-indicator", Static).display = False
        self.query_one("#fr-collapse", Button).display = False

        n = len(self._vm._cards)
        self.query_one("#fr-start-summary", Static).update(
            f"{n} card{'s' if n != 1 else ''} to review"
        )

        self._reconcile_timer_interval()
        self._reconcile_due_interval()
        self._reconcile_throbber_interval()

    def _refresh_reviewing(self) -> None:
        self.query_one("#fr-start", Vertical).display = False
        self.query_one("#fr-card", Vertical).display = True
        self.query_one("#fr-done", Vertical).display = False
        self.query_one("#fr-collapse", Button).display = False

        indicator = self.query_one("#fr-batch-indicator", Static)
        indicator.display = self._vm.autoscore_in_progress
        if self._vm.autoscore_in_progress:
            indicator.update(self._batch_indicator_text())

        self._refresh_current_card()

    def _refresh_done(self) -> None:
        self.query_one("#fr-start", Vertical).display = False
        self.query_one("#fr-done", Vertical).display = True
        self.query_one("#fr-batch-indicator", Static).display = False

        btn = self.query_one("#fr-collapse", Button)
        btn.display = True
        btn.label = "▶" if self._vm.collapsed else "▼"

        status = self.query_one("#fr-done-status", Static)
        if self._vm.cancelled:
            status.update(f"[{_CANCEL_RED}]Session cancelled[/]")
        else:
            text = "Session complete"
            # Show count only in the collapsed summary view.
            if self._vm.collapsed:
                reviewed = sum(
                    1 for c in self._vm._cards
                    if c.scored and c.score != Flashcard.Score.SKIPPED
                )
                text = (
                    f"Session complete — {reviewed} "
                    f"card{'s' if reviewed != 1 else ''} reviewed"
                )
            status.update(f"[{_DONE_GREEN}]{text}[/]")

        # When expanded, keep the card visible so the user can browse
        # through their scored cards with alt+←/→.
        card_visible = not self._vm.collapsed
        self.query_one("#fr-card", Vertical).display = card_visible
        if card_visible:
            self._refresh_current_card()
        else:
            # Card hidden — nothing to tick.
            self._reconcile_timer_interval()
            self._reconcile_due_interval()
            self._reconcile_throbber_interval()

    def _refresh_current_card(self) -> None:
        """Dispatcher: shared header + dots + intervals around a per-state body renderer.

        Each ``_refresh_current_card_<state>`` method owns the visibility and content of the
        card body (question / answer-input / user-answer / answer / rule / below-text) for
        its state — the helpers below (``_show_question`` / ``_hide_question`` /
        ``_sync_answer_input_visibility`` / ``_show_revealed_panel`` /
        ``_hide_revealed_panel``) hide the per-widget plumbing so each state method reads as
        a flat declaration of "what's visible right now".
        """
        card = self._vm.current_card
        if card is None:
            return

        # Trailing star when the user has flagged this card for later review.
        star = f" [bold {_APPROVAL_YELLOW}]*[/]" if card.flagged else ""
        self.query_one("#fr-question-label", Static).update(
            f"Question{star}  [dim](id {card.id})[/dim]"
        )
        self.query_one("#fr-counter", Static).update(self._counter_text(card))

        match card.state:
            case Flashcard.State.FRONT:
                self._refresh_current_card_front(card)
            case Flashcard.State.REVEALED_NOT_SCORED:
                self._refresh_current_card_revealed_not_scored(card)
            case Flashcard.State.REVEALED_PENDING_AUTO_SCORE:
                self._refresh_current_card_revealed_pending_auto_score(card)
            case Flashcard.State.SCORED_PENDING_APPROVAL:
                self._refresh_current_card_scored_pending_approval(card)
            case Flashcard.State.SCORED:
                self._refresh_current_card_scored(card)
            case Flashcard.State.AWAITING_REVEAL:
                self._refresh_current_card_awaiting_reveal(card)

        self.query_one("#fr-dots", _DotStrip).update_state(
            self._vm._cards, self._vm._current_card_index
        )

        self._reconcile_timer_interval()
        self._reconcile_due_interval()
        self._reconcile_throbber_interval()

    # ------------------------------------------------------------------
    # Per-state body renderers
    # ------------------------------------------------------------------

    def _refresh_current_card_front(self, card: Flashcard) -> None:
        self._show_question(card)
        self._sync_answer_input_visibility(input_visible=True, card=card)
        self._hide_revealed_panel()
        self.query_one("#fr-below", Static).update(
            "Type your answer and press [bold]enter[/bold] to reveal"
        )

    def _refresh_current_card_revealed_not_scored(self, card: Flashcard) -> None:
        self._show_question(card)
        self._sync_answer_input_visibility(input_visible=False, card=card)
        self._show_revealed_panel(card, show_answer=True)
        self.query_one("#fr-below", Static).update(self._rating_row_text(card))

    def _refresh_current_card_revealed_pending_auto_score(self, card: Flashcard) -> None:
        self._show_question(card)
        self._sync_answer_input_visibility(input_visible=False, card=card)
        self._show_revealed_panel(card, show_answer=True)

        if self._vm.autoscore_in_progress:
            hint_text = "pending score..."
        else:
            hint_text = "press 1-4 to override"
            
        self.query_one("#fr-below", Static).update(
            f"[{_RATING_LABEL_DIM}]Queued for auto-scoring  —  "
            f"{hint_text}[/]"
        )

    def _refresh_current_card_scored_pending_approval(self, card: Flashcard) -> None:
        self._show_question(card)
        self._sync_answer_input_visibility(input_visible=False, card=card)
        self._show_revealed_panel(card, show_answer=True)
        self.query_one("#fr-below", Static).update(self._pending_approval_text(card))

    def _refresh_current_card_scored(self, card: Flashcard) -> None:
        self._show_question(card)
        self._sync_answer_input_visibility(input_visible=False, card=card)
        # Skipped cards: hide the answer so the user can't skip-then-reset to peek.
        is_skipped = card.score == Flashcard.Score.SKIPPED
        self._show_revealed_panel(card, show_answer=not is_skipped)
        label = card.score.name.lower() if card.score else "?"
        self.query_one("#fr-below", Static).update(
            f"[{_RATING_LABEL_DIM}]Scored: {label}[/]"
        )

    def _refresh_current_card_awaiting_reveal(self, card: Flashcard) -> None:
        # Face-down: hide question entirely; only the header + countdown remain.
        self._hide_question()
        self._sync_answer_input_visibility(input_visible=False, card=card)
        self._hide_revealed_panel()
        self.query_one("#fr-below", Static).update(self._awaiting_reveal_text(card))

    # ------------------------------------------------------------------
    # Per-piece show/hide helpers (used by the state renderers above)
    # ------------------------------------------------------------------

    def _show_question(self, card: Flashcard) -> None:
        w = self.query_one("#fr-question", Static)
        w.display = True
        w.update(card.question)

    def _hide_question(self) -> None:
        self.query_one("#fr-question", Static).display = False

    def _sync_answer_input_visibility(
        self, *, input_visible: bool, card: Flashcard
    ) -> None:
        """Show/hide the answer input + label, sync its buffer to the card's stored draft,
        and route focus appropriately.

        The input is only ever visible during REVIEWING — hiding it in DONE (e.g. after
        cancellation) prevents the input from remaining focusable past session end. The
        draft sync suppresses the echo back through ``on_text_area_changed``.

        Focus management: route focus to the input when it's visible (so typing lands
        there), to ``self`` otherwise (so enter/1-4/nav keys reach ``on_key``). Only
        move focus if we already own it somewhere — don't steal from the chat input or
        anywhere else outside this widget.
        """
        in_review = self._vm.state == FlashcardReviewVM.State.REVIEWING
        show = input_visible and in_review

        self.query_one("#fr-answer-input-label", Static).display = show
        answer_input = self.query_one("#fr-answer-input", _AnswerInput)
        answer_input.display = show
        if show:
            draft = card.user_answer or ""
            if answer_input.text != draft:
                self._suppress_text_change = True
                try:
                    answer_input.load_text(draft)
                finally:
                    self._suppress_text_change = False

        app_focused = self.app.focused
        we_own_focus = app_focused is self or app_focused is answer_input
        if not we_own_focus:
            return
        if show and app_focused is not answer_input and not self._vm.cancelled:
            answer_input.focus()
        elif not show and app_focused is answer_input:
            self.focus()

    def _show_revealed_panel(self, card: Flashcard, *, show_answer: bool) -> None:
        """Show the user's submitted answer (if non-empty) and optionally the revealed
        answer. The separator rule appears only when both panels are visible.

        ``show_answer=False`` is the SKIPPED case (and any future state where the user
        shouldn't see the answer): the user-answer panel still renders if there's a draft
        to show, but the revealed-answer panel and rule stay hidden.
        """
        show_user_answer = bool(card.user_answer)
        self.query_one("#fr-user-answer-label", Static).display = show_user_answer
        user_widget = self.query_one("#fr-user-answer", Static)
        user_widget.display = show_user_answer
        if show_user_answer:
            user_widget.update(card.user_answer or "")

        self.query_one("#fr-answer-label", Static).display = show_answer
        answer_widget = self.query_one("#fr-answer", Static)
        answer_widget.display = show_answer
        if show_answer:
            answer_widget.update(card.answer)

        self.query_one("#fr-ua-rule", Rule).display = show_user_answer and show_answer

    def _hide_revealed_panel(self) -> None:
        self.query_one("#fr-user-answer-label", Static).display = False
        self.query_one("#fr-user-answer", Static).display = False
        self.query_one("#fr-answer-label", Static).display = False
        self.query_one("#fr-answer", Static).display = False
        self.query_one("#fr-ua-rule", Rule).display = False

    # ------------------------------------------------------------------
    # Text builders
    # ------------------------------------------------------------------

    def _counter_text(self, card: Flashcard) -> str:
        position = f"{self._vm._current_card_index + 1}/{len(self._vm._cards)}"
        suffix = self._remaining_suffix()
        if self._vm.timers_visible:
            # Elapsed time is frozen on non-FRONT cards (think-time timer is
            # paused on transitions out of FRONT); on FRONT it ticks live via
            # _reconcile_timer_interval.
            return f"{card.elapsed_time:.1f}s  ·  {position}{suffix}"
        if card.state != Flashcard.State.FRONT:
            return f"{position}{suffix}"
        # FRONT without the timer visible — throbber in its place.
        char, color = _THROBBER_FRAMES[self._throbber_frame]
        return f"[{color}]{char}[/]  ·  {position}{suffix}"

    def _remaining_suffix(self) -> str:
        total = self._vm.num_remaining
        if total == 0:
            return ""
        position = self._vm.remaining_position
        inner = f"{position}/{total}" if position is not None else f"-/{total}"
        return f"  [dim]({inner} remaining)[/dim]"

    def _batch_indicator_text(self) -> str:
        char, color = _THROBBER_FRAMES[self._throbber_frame]
        return f"[{color}]{char}[/] Auto-scoring…"

    def _rating_row_text(self, card: Flashcard) -> str:
        # Two ways the auto-score path can be suppressed for this attempt: the scorer
        # failed on it (auto_scoring_failed), or the user rejected the staged proposal
        # via 'd' (auto_score_discarded). Both fall back to enter = good and surface a
        # contextual "rate manually" prefix; failed wins if both are somehow set since
        # it's the more system-level signal.
        if card.auto_scoring_failed:
            prefix = f"[{_FAIL_RED}]Auto-score failed — rate manually:[/]  "
        elif card.auto_score_discarded:
            prefix = f"[{_APPROVAL_YELLOW}]Auto-score rejected — rate manually:[/]  "
        else:
            prefix = ""
        enter_label = "auto" if self._vm.auto_score_active_for_current_card else "good"
        previews = card.rating_previews()
        pairs = [
            (1, "again", Rating.Again),
            (2, "hard", Rating.Hard),
            (3, "good", Rating.Good),
            (4, "easy", Rating.Easy),
        ]
        segments = [
            f"[bold {_RATING_COLORS[num]}]{num}[/] "
            f"[{_RATING_LABEL_DIM}]{label}[/] "
            f"[{_HINT_DIM}]({_format_due(previews[rating])})[/]"
            for num, label, rating in pairs
        ]
        row = "    ".join(segments)
        return f"{prefix}{row}    [{_HINT_DIM}]\\[enter = {enter_label}][/]"

    def _pending_approval_text(self, card: Flashcard) -> str:
        """Two-line below text for SCORED_PENDING_APPROVAL: the same four-rating row as
        the manual rating row, but with the auto-scorer's proposed rating wrapped in
        brackets to highlight it; below that, an action hint listing approve / reject /
        manually-score keys.

        The cached rating previews populated at the FRONT -> REVEALED_NOT_SCORED reveal
        are still valid here (FSRS state is intentionally not advanced while the rating
        sits awaiting approval).
        """
        previews = card.rating_previews()
        pending = card.pending_score
        pairs = [
            (Flashcard.Score.AGAIN, 1, "again", Rating.Again),
            (Flashcard.Score.HARD,  2, "hard",  Rating.Hard),
            (Flashcard.Score.GOOD,  3, "good",  Rating.Good),
            (Flashcard.Score.EASY,  4, "easy",  Rating.Easy),
        ]
        segments = []
        for score, num, label, rating in pairs:
            interval = f"[{_HINT_DIM}]({_format_due(previews[rating])})[/]"
            digit = f"[bold {_RATING_COLORS[num]}]{num}[/]"
            if score == pending:
                # Selected: brackets + label promoted to the approval yellow, digit stays
                # in its rating color so the rating-color vocabulary still reads, plus a
                # leading chevron in the same yellow as a directional anchor.
                bright_label = f"[bold {_APPROVAL_YELLOW}]{label}[/]"
                bracket_open = f"[bold {_APPROVAL_YELLOW}]\\[[/]"
                bracket_close = f"[bold {_APPROVAL_YELLOW}]][/]"
                chevron = f"[bold {_APPROVAL_YELLOW}]▸[/]"
                segments.append(
                    f"{chevron} {bracket_open} {digit} {bright_label} {interval} {bracket_close}"
                )
            else:
                dim_label = f"[{_RATING_LABEL_DIM}]{label}[/]"
                segments.append(f"{digit} {dim_label} {interval}")
        row = "    ".join(segments)

        # Bold yellow for [approve]
        approve_binding     = self._key_for("approve_auto_score")
        approve_all_binding = self._key_for("accept_all_auto_scores")
        reject_binding      = self._key_for("reject_auto_score")

        hint = (
            f"[bold {_APPROVAL_YELLOW}]\\[{approve_binding}] to approve[/]  ·  "
            f"[{_APPROVAL_YELLOW}]\\[{approve_all_binding}][/] [{_RATING_LABEL_DIM}]approve all  ·  "
            f"\\[{reject_binding}] to reject  ·  "
            f"\\[1-4] to manually score[/]"
        )
        return f"{row}\n\n{hint}"

    def _awaiting_reveal_text(self, card: Flashcard) -> str:
        due_in = card.due_in or 0.0
        return f"Due in {due_in:.0f}s — press [bold]enter[/bold] to reveal"

    # ------------------------------------------------------------------
    # Interval reconciliation + tick callbacks
    # ------------------------------------------------------------------

    def _reconcile_timer_interval(self) -> None:
        """Start or stop the live-timer ticker so it runs exactly when
        there's a live elapsed value to display."""
        card = self._vm.current_card
        live_timer_visible = (
            card is not None
            and card.state == Flashcard.State.FRONT
            and self._vm.timers_visible
            and self._vm.state == FlashcardReviewVM.State.REVIEWING
        )
        currently_running = self._timer_interval is not None
        if live_timer_visible and not currently_running:
            self._timer_interval = self.set_interval(0.1, self._tick_timer)
        elif currently_running and not live_timer_visible:
            self._timer_interval.stop()
            self._timer_interval = None

    def _tick_timer(self) -> None:
        card = self._vm.current_card
        if card is None:
            return
        self.query_one("#fr-counter", Static).update(self._counter_text(card))

    def _reconcile_due_interval(self) -> None:
        """Start or stop the due-countdown ticker so it runs exactly when
        the current card is AWAITING_REVEAL."""
        card = self._vm.current_card
        countdown_visible = (
            card is not None
            and card.state == Flashcard.State.AWAITING_REVEAL
            and self._vm.state == FlashcardReviewVM.State.REVIEWING
        )
        currently_running = self._due_interval is not None
        if countdown_visible and not currently_running:
            self._due_interval = self.set_interval(1.0, self._tick_due)
        elif currently_running and not countdown_visible:
            self._due_interval.stop()
            self._due_interval = None

    def _tick_due(self) -> None:
        card = self._vm.current_card
        if card is None or card.state != Flashcard.State.AWAITING_REVEAL:
            return
        self.query_one("#fr-below", Static).update(self._awaiting_reveal_text(card))

    def _reconcile_throbber_interval(self) -> None:
        """Start or stop the shared throbber ticker. Runs when either the
        think-time throbber (FRONT card without timer visible) or the
        batch-scoring indicator needs to animate."""
        card = self._vm.current_card
        in_review = self._vm.state == FlashcardReviewVM.State.REVIEWING
        think_throbber = (
            in_review
            and card is not None
            and card.state == Flashcard.State.FRONT
            and not self._vm.timers_visible
        )
        batch_throbber = in_review and self._vm.autoscore_in_progress
        throbber_visible = think_throbber or batch_throbber
        currently_running = self._throbber_interval is not None
        if throbber_visible and not currently_running:
            self._throbber_interval = self.set_interval(0.15, self._tick_throbber)
        elif currently_running and not throbber_visible:
            self._throbber_interval.stop()
            self._throbber_interval = None

    def _tick_throbber(self) -> None:
        self._throbber_frame = (self._throbber_frame + 1) % len(_THROBBER_FRAMES)
        card = self._vm.current_card
        # Counter-position throbber.
        if (
            card is not None
            and card.state == Flashcard.State.FRONT
            and not self._vm.timers_visible
        ):
            self.query_one("#fr-counter", Static).update(self._counter_text(card))
        # Batch-indicator throbber.
        if self._vm.autoscore_in_progress:
            self.query_one("#fr-batch-indicator", Static).update(
                self._batch_indicator_text()
            )
