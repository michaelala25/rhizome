"""FlashcardReview — thin Textual view over FlashcardReviewViewModel."""

from __future__ import annotations

from typing import Any

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Rule, Static, TextArea

from fsrs import Rating

from .view_model import (
    Action,
    Flashcard,
    FlashcardData,
    FlashcardReviewViewModel,
    KEYBINDINGS,
)
from ..interrupt import InterruptWidgetBase


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

# Dot-strip colors. Cursor uses a different glyph (◉) so it reads even when the
# card under it is also colored (e.g. blue while pending auto-score).
_DOT_UNSCORED = "rgb(220,220,220)"
_DOT_DONE = "rgb(85,85,85)"
_DOT_PENDING = "rgb(120,160,230)"
_DOT_FAILED = "rgb(235,100,100)"
_DOT_CHEVRON = "rgb(110,110,110)"
_DOT_GLYPH = "•"
_DOT_CURSOR_GLYPH = "◉"


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


class _DotStrip(Static):
    """Bottom-of-widget progress strip — one dot per card.

    Color encodes per-card status (unscored / done / pending auto-score /
    auto-score failed). The card under the cursor gets a different glyph
    so it stays visible regardless of color.

    When the card list is wider than the available content width, the
    strip scrolls to keep the cursor visible and shows ``<`` / ``>``
    chevrons on the truncated side(s).
    """

    def __init__(self, max_dots=10, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._cards: list[Flashcard] = []
        self._cursor: int = 0
        self._scroll: int = 0
        self._max_dots = max_dots

    def update_state(self, cards: list[Flashcard], cursor: int) -> None:
        self._cards = cards
        self._cursor = cursor
        self._redraw()

    def on_resize(self, event: events.Resize) -> None:
        self._redraw()

    def _redraw(self) -> None:
        n = len(self._cards)
        if n == 0:
            self.update("")
            return

        # Each dot is glyph + space (2 chars). Reserve 2 chars on each
        # side for chevrons even when not shown, so dots don't shift
        # horizontally as scroll state changes.
        width = self.size.width or 0
        if width <= 0:
            # Pre-mount / pre-layout — fall back to the cap; resize will refit.
            width_cap = self._max_dots
        else:
            width_cap = max(1, (width - 4) // 2)
        visible = min(n, self._max_dots, width_cap)

        # Reconcile scroll so the cursor stays in the window.
        if self._cursor < self._scroll:
            self._scroll = self._cursor
        elif self._cursor >= self._scroll + visible:
            self._scroll = self._cursor - visible + 1
        self._scroll = max(0, min(self._scroll, n - visible))

        start = self._scroll
        end = start + visible
        left = f"[{_DOT_CHEVRON}]<[/]" if start > 0 else " "
        right = f"[{_DOT_CHEVRON}]>[/]" if end < n else " "
        dots = " ".join(
            self._dot(self._cards[i], i == self._cursor)
            for i in range(start, end)
        )
        self.update(f"{left} {dots} {right}")

    @staticmethod
    def _dot(card: Flashcard, is_cursor: bool) -> str:
        glyph = _DOT_CURSOR_GLYPH if is_cursor else _DOT_GLYPH
        if card.auto_scoring_failed and card.state != Flashcard.State.SCORED:
            color = _DOT_FAILED
        elif card.state == Flashcard.State.REVEALED_PENDING_AUTO_SCORE:
            color = _DOT_PENDING
        elif card.state == Flashcard.State.SCORED:
            color = _DOT_DONE
        else:
            color = _DOT_UNSCORED
        return f"[{color}]{glyph}[/]"


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
        # (alt+x reset, alt+s skip, alt+h help, alt+left/right nav) don't
        # get swallowed as literal text input.
        if event.key.startswith("alt+"):
            event.prevent_default()


class FlashcardReview(InterruptWidgetBase):

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
    FlashcardReview #fr-bottom-row {
        height: 1;
        margin: 1 0 0 0;
    }
    FlashcardReview #fr-bottom-row > Static,
    FlashcardReview #fr-bottom-row > _DotStrip {
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

    @classmethod
    def from_interrupt(cls, value: dict[str, Any], context: Any = None) -> "FlashcardReview":
        """Construct from an interrupt value dict and the AgentContext.

        Pulls the auto-scorer and DB session factory off the context.
        FSRS state mutates entirely in-memory during the session; the
        session factory is held only so the VM's optional ``commit()``
        API can be called by a consumer of the resolved payload.
        """
        scorer = getattr(context, "scorer_subagent", None) if context is not None else None
        session_factory = getattr(context, "session_factory", None) if context is not None else None
        return cls(
            cards=value["cards"],
            session_factory=session_factory,
            auto_score_enabled=value.get("auto_score_enabled", False),
            auto_scorer=scorer,
        )

    def __init__(
        self,
        cards: list[FlashcardData],
        session_factory: Any,
        auto_score_enabled: bool = False,
        auto_scorer: Any = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._vm = FlashcardReviewViewModel(
            cards=cards,
            session_factory=session_factory,
            auto_score_enabled=auto_score_enabled,
            auto_scorer=auto_scorer,
        )
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
            with Horizontal(id="fr-bottom-row"):
                yield Static("", id="fr-help-hint")
                yield _DotStrip(id="fr-dots")
                yield Static("", id="fr-bottom-spacer")

        with Vertical(id="fr-done"):
            yield Static("", id="fr-done-status")

        yield Static("", id="fr-help")

    def on_mount(self) -> None:
        super().on_mount()  # InterruptWidgetBase → setup_navigation

        self._vm.dirty.append(self._refresh)
        self._vm.dirty.append(self._maybe_resolve)

        # Border-title nav hint on the card container.
        card_container = self.query_one("#fr-card", Vertical)
        card_container.border_title = "alt+←/→ to navigate"
        card_container.styles.border_title_align = "right"

        self._refresh()

    def action_cancel_interrupt(self) -> None:
        """No-op. Ctrl+c is owned by the VM's ``on_key`` handler, which
        transitions the VM to its DONE(cancelled=True) state;
        ``_maybe_resolve`` then cancels the underlying future.

        If this also called ``self._vm.cancel()``, Textual would fire
        BOTH this binding action AND the ``on_key`` event for the same
        keypress, and the second call would hit ``vm.finish``'s
        ``state != DONE`` assertion.

        TODO: this no-op override is a band-aid. Eventually the ctrl+c
        binding should be removed from ``InterruptWidgetBase`` entirely
        — each widget's cancellation UX is different enough that the
        base shouldn't presume one. Related: the base's ``.cancel()``
        shadows what looks like a generic method name; renaming it to
        something more specific (``.cancel_interrupt()``, or ideally
        folding it into ``.resolve(cancelled=True)``) would remove the
        footgun where a subclass might unintentionally override it.
        """
        pass

    def _maybe_resolve(self) -> None:
        """Called on every VM ``dirty`` emit. The first time we observe
        the VM reach its DONE state, resolve the future with the result.
        
        Cancel and complete both resolve — ``_build_result`` sets
        ``completed`` accordingly so the tool can distinguish them and
        handle partial state. Subsequent emits after DONE (collapse
        toggle, navigation while expanded) are no-ops because
        ``_future`` is already done.
        """
        if self._future.done():
            return
        if self._vm.state != FlashcardReviewViewModel.State.DONE:
            return
        self.resolve(self._build_result())

    def _build_result(self) -> dict[str, Any]:
        cards = []
        
        for card in self._vm._cards:
            score_val: int | None = None
            score_label: str | None = None
            score = card.score

            if score in (
                Flashcard.Score.AGAIN,
                Flashcard.Score.HARD,
                Flashcard.Score.GOOD,
                Flashcard.Score.EASY,
            ):
                score_val = score.value
                score_label = score.name.lower()
            elif score == Flashcard.Score.SKIPPED:
                score_label = "skipped"
            elif score == Flashcard.Score.AUTO:
                # Session ended while the card was still pending a batch
                # (e.g. user cancelled mid-batch). No final rating.
                score_label = "auto"

            cards.append({
                "id": card.id,
                "question": card.question,
                "answer": card.answer,
                "user_answer": card.user_answer or "",
                "score": score_val,
                "score_label": score_label,
                "duration": round(card.elapsed_time, 1),
                # Final in-memory FSRS state for this card. The widget
                # never persists this itself — the consumer (typically
                # the review_present_flashcards tool) decides whether to
                # commit it to the DB based on the session's ephemeral
                # flag.
                "fsrs_card": card.fsrs_card,
            })

        return {
            "completed": not self._vm.cancelled,
            "cards": cards,
        }

    def on_focus(self, event: events.Focus) -> None:
        """When the widget gains focus from outside (e.g. navigation via
        ctrl+up/down), route focus into the answer input if the current
        card is in FRONT. Without this, the focus-shift logic inside
        ``_refresh_current_card`` never runs for externally-triggered
        focus changes (no VM dirty emit is fired)."""
        card = self._vm.current_card
        if card is None:
            return
        if (
            card.state == Flashcard.State.FRONT
            and self._vm.state == FlashcardReviewViewModel.State.REVIEWING
        ):
            self.query_one("#fr-answer-input", _AnswerInput).focus()

    def on_key(self, event: events.Key) -> None:
        self._vm.on_key(event)

    def on__answer_input_submitted(
        self, event: _AnswerInput.Submitted
    ) -> None:
        # Route the enter keypress the TextArea swallowed up to the VM.
        self._vm.on_key(events.Key("enter", "\r"))

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
    # Rendering
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        match self._vm.state:
            case FlashcardReviewViewModel.State.START:
                self._refresh_start()
            case FlashcardReviewViewModel.State.REVIEWING:
                self._refresh_reviewing()
            case FlashcardReviewViewModel.State.DONE:
                self._refresh_done()
        self._refresh_help()

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
            hint.update(f"[bold]{KEYBINDINGS[Action.TOGGLE_HELP]}[/]  show help")

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
            (Action.TOGGLE_HELP, "hide help"),
            (Action.CANCEL, "cancel session"),
            (Action.TOGGLE_TIMER, "toggle timer"),
            (Action.RESET_CARD, "reset current card"),
            (Action.TOGGLE_SKIP, "skip / unskip card"),
            (Action.TOGGLE_AUTO_SCORE, auto_label),
        ]
        return "    ".join(
            f"[bold]{KEYBINDINGS[action]}[/]  {label}"
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
        card = self._vm.current_card
        if card is None:
            return

        self.query_one("#fr-question-label", Static).update(
            f"Question  [dim](id {card.id})[/dim]"
        )
        self.query_one("#fr-counter", Static).update(self._counter_text(card))
        # Question body hidden when face-down (AWAITING_REVEAL); only the
        # header + countdown remain visible.
        question_visible = card.state != Flashcard.State.AWAITING_REVEAL
        question_widget = self.query_one("#fr-question", Static)
        question_widget.display = question_visible
        if question_visible:
            question_widget.update(card.question)

        # Answer input — visible only in FRONT during an active session.
        # Hiding it in DONE (e.g. post-cancellation) prevents the input
        # from remaining focusable after the session has ended. Syncs its
        # buffer to the card's stored draft, suppressing the echo back
        # through on_text_area_changed.
        input_visible = (
            card.state == Flashcard.State.FRONT
            and self._vm.state == FlashcardReviewViewModel.State.REVIEWING
        )
        self.query_one("#fr-answer-input-label", Static).display = input_visible
        answer_input = self.query_one("#fr-answer-input", _AnswerInput)
        answer_input.display = input_visible
        if input_visible:
            draft = card.user_answer or ""
            if answer_input.text != draft:
                self._suppress_text_change = True
                try:
                    answer_input.load_text(draft)
                finally:
                    self._suppress_text_change = False

        # Route focus based on state: input when FRONT (so typing works),
        # self otherwise (so enter/1-4/nav keys reach vm.on_key). Only move
        # focus if we already own it somewhere — don't steal from the chat
        # input or anywhere else outside this widget.
        app_focused = self.app.focused
        we_own_focus = app_focused is self or app_focused is answer_input
        if we_own_focus:
            if input_visible and app_focused is not answer_input and not self._vm.cancelled:
                answer_input.focus()
            elif not input_visible and app_focused is answer_input:
                self.focus()

        # User's submitted answer — shown once the card is revealed.
        revealed_states = {
            Flashcard.State.REVEALED_NOT_SCORED,
            Flashcard.State.REVEALED_PENDING_AUTO_SCORE,
            Flashcard.State.SCORED,
        }
        show_user_answer = card.state in revealed_states and bool(card.user_answer)
        self.query_one("#fr-user-answer-label", Static).display = show_user_answer
        user_answer_widget = self.query_one("#fr-user-answer", Static)
        user_answer_widget.display = show_user_answer
        if show_user_answer:
            user_answer_widget.update(card.user_answer or "")

        # Revealed answer. Skipped cards stay hidden so a user can't
        # skip-then-reset to peek at the answer.
        is_skipped = (
            card.state == Flashcard.State.SCORED
            and card.score == Flashcard.Score.SKIPPED
        )
        show_answer = card.state in revealed_states and not is_skipped
        self.query_one("#fr-answer-label", Static).display = show_answer
        answer_widget = self.query_one("#fr-answer", Static)
        answer_widget.display = show_answer
        if show_answer:
            answer_widget.update(card.answer)

        # Separator between user-answer and revealed answer — only when both visible.
        self.query_one("#fr-ua-rule", Rule).display = show_user_answer and show_answer

        # Below-card status text.
        below = self.query_one("#fr-below", Static)
        match card.state:
            case Flashcard.State.FRONT:
                below.update(
                    "Type your answer and press [bold]enter[/bold] to reveal"
                )
            case Flashcard.State.REVEALED_NOT_SCORED:
                below.update(self._rating_row_text(card))
            case Flashcard.State.REVEALED_PENDING_AUTO_SCORE:
                below.update(
                    f"[{_RATING_LABEL_DIM}]Queued for auto-scoring  —  "
                    f"press 1-4 to override[/]"
                )
            case Flashcard.State.SCORED:
                label = card.score.name.lower() if card.score else "?"
                below.update(f"[{_RATING_LABEL_DIM}]Scored: {label}[/]")
            case Flashcard.State.AWAITING_REVEAL:
                below.update(self._awaiting_reveal_text(card))

        self.query_one("#fr-dots", _DotStrip).update_state(
            self._vm._cards, self._vm._current_card_index
        )

        self._reconcile_timer_interval()
        self._reconcile_due_interval()
        self._reconcile_throbber_interval()

    # ------------------------------------------------------------------
    # Text builders
    # ------------------------------------------------------------------

    def _counter_text(self, card: Flashcard) -> str:
        position = f"{self._vm._current_card_index + 1}/{len(self._vm._cards)}"
        suffix = self._remaining_suffix()
        if card.state != Flashcard.State.FRONT:
            return f"{position}{suffix}"
        if card.timer_visible:
            return f"{card.elapsed_time:.1f}s  ·  {position}{suffix}"
        # Throbber in place of the timer.
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
        prefix = (
            f"[{_FAIL_RED}]Auto-score failed — rate manually:[/]  "
            if card.auto_scoring_failed else ""
        )
        enter_label = (
            "auto"
            if self._vm.auto_score_enabled and not card.auto_scoring_failed
            else "good"
        )
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
            and card.timer_visible
            and self._vm.state == FlashcardReviewViewModel.State.REVIEWING
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
            and self._vm.state == FlashcardReviewViewModel.State.REVIEWING
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
        in_review = self._vm.state == FlashcardReviewViewModel.State.REVIEWING
        think_throbber = (
            in_review
            and card is not None
            and card.state == Flashcard.State.FRONT
            and not card.timer_visible
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
            and not card.timer_visible
        ):
            self.query_one("#fr-counter", Static).update(self._counter_text(card))
        # Batch-indicator throbber.
        if self._vm.autoscore_in_progress:
            self.query_one("#fr-batch-indicator", Static).update(
                self._batch_indicator_text()
            )
