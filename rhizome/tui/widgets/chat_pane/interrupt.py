"""InterruptViewModelBase + TestInterrupt for the chat-pane MVVM rewrite.

Interrupts are feed-native sub-VMs: they live as siblings of AgentMessage in the chat pane's feed, not as
children of it. The peek-tail rule means an interrupt appearing implicitly closes the open AgentMessage
(since the feed tail is no longer one), and the next agent chunk opens a fresh one.

The future lives on the VM, not on the widget. ``present_interrupt`` on the chat pane awaits
``vm.wait_for_selection()``; view-side mutator calls (``move_cursor``, ``confirm``) eventually trigger
``resolve(value)``, which sets the future. This survives view re-mount, decouples the awaiter from Textual,
and keeps the interrupt unit-testable without a Textual app.

Mutators are idempotent on the resolved state: stale key handlers can't double-fire the future, and
``cancel`` after ``resolve`` is a no-op.
"""

from __future__ import annotations

import asyncio
from typing import Any

from textual.app import ComposeResult
from textual.widgets import Static

from ..view_base import ViewBase
from ..view_model_base import ViewModelBase


class InterruptViewModelBase(ViewModelBase):
    """Common machinery for any feed-resident interrupt VM. Subclasses define the interaction surface
    (cursor, options, etc.); the base owns the future plumbing + idempotency guards.
    """

    def __init__(self) -> None:
        super().__init__()
        self._future: asyncio.Future[Any] = asyncio.Future()
        self.resolved: bool = False
        self.cancelled: bool = False
        self.result: Any | None = None

    async def future(self) -> Any:
        """Block until the user resolves (or cancels) this interrupt. Returns the resolved value, or raises
        ``asyncio.CancelledError`` on cancel.
        """
        return await self._future

    def resolve(self, value: Any) -> None:
        """Set the future to ``value``. No-op if already resolved/cancelled."""
        if self.resolved:
            return

        self.resolved = True
        self.result = value
        if not self._future.done():
            self._future.set_result(value)

        self.emit(self.dirty)

    def cancel(self) -> None:
        """Cancel the future (the awaiter sees ``CancelledError``). No-op if already resolved or cancelled."""
        if self.resolved:
            return

        self.resolved = True
        self.cancelled = True
        if not self._future.done():
            self._future.cancel()

        self.emit(self.dirty)


class TestInterruptViewModel(InterruptViewModelBase):
    """Minimal interrupt: a cursor over a list of options, Enter resolves with the selected option's value.
    Built for routing verification — not linked to any tool call.
    """

    def __init__(self, prompt: str = "Choose one:", options: list[str] | None = None) -> None:
        super().__init__()
        self.prompt = prompt
        self.options = options or ["ok", "cancel"]
        self.cursor: int = 0

    def move_cursor(self, delta: int) -> None:
        if self.resolved:
            return
        new = (self.cursor + delta) % len(self.options)
        if new == self.cursor:
            return
        self.cursor = new
        self.emit(self.dirty)

    def confirm(self) -> None:
        if self.resolved:
            return
        self.resolve(self.options[self.cursor])


class TestInterruptView(ViewBase[TestInterruptViewModel]):
    """Single-Static projection of ``TestInterruptViewModel``. Up/Down move the cursor; Enter confirms. The
    widget keeps itself rendered after resolution so the conversational record shows what was chosen.
    """

    DEFAULT_CSS = """
    TestInterruptView {
        height: auto;
        padding: 1 2;
        margin: 0 2;
        border: round rgb(80, 80, 80);
    }
    TestInterruptView:focus {
        border: round rgb(140, 140, 200);
    }
    TestInterruptView.--resolved {
        border: round rgb(50, 50, 50);
        color: $text-muted;
    }
    TestInterruptView #interrupt-body {
        width: 1fr;
        height: auto;
    }
    """

    BINDINGS = [
        ("up", "move_cursor(-1)", "Up"),
        ("down", "move_cursor(1)", "Down"),
        ("enter", "confirm", "Confirm"),
    ]

    can_focus = True

    def compose(self) -> ComposeResult:
        yield Static("", id="interrupt-body")

    def on_mount(self) -> None:
        # Take focus on appearance so keys route here without needing a click.
        self.focus()
        self._refresh()

    def _refresh(self) -> None:
        lines: list[str] = [self._vm.prompt, ""]

        for i, opt in enumerate(self._vm.options):
            marker = ">" if (i == self._vm.cursor and not self._vm.resolved) else " "
            lines.append(f"{marker} {opt}")

        if self._vm.resolved:
            tail = "(cancelled)" if self._vm.cancelled else f"chose: {self._vm.result}"
            lines.extend(["", tail])
            self.add_class("--resolved")

        self.query_one("#interrupt-body", Static).update("\n".join(lines))

    def action_move_cursor(self, delta: int) -> None:
        self._vm.move_cursor(delta)

    def action_confirm(self) -> None:
        self._vm.confirm()
