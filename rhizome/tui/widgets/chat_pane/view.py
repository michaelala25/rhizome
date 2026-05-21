"""ChatPane view — steps 1+2 of the MVVM rewrite.

Layout: a ``VerticalScroll`` feed, a ``ChatInputView`` bound to
``vm.chat_input``, and a ``CommandPalette`` bound to the shared
``vm.command_palette``. The view subscribes to the VM's ``feed_append``
to mount one widget per appended feed entry and to ``feed_clear`` to
drop them all. All input-area keystroke handling (Enter, Tab, Up, Down,
Escape, Ctrl+Enter) lives inside ``ChatInputView`` itself, which talks
to the input VM directly — the pane is no longer in the path.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll

from textual.widget import Widget

from ..view_base import ViewBase
from .agent_message import AgentMessageView, AgentMessageViewModel
from .branch_indicator import BranchIndicatorView, BranchIndicatorViewModel
from .chat_input import ChatInputView
from .chat_message import ChatMessageView
from .choices import ChoicesView, ChoicesViewModel
from .command_palette import CommandPalette
from .interrupt import InterruptViewModelBase, TestInterruptView, TestInterruptViewModel
from .multiple_choices import MultipleChoicesView, MultipleChoicesViewModel
from .sql_confirmation import SqlConfirmationView, SqlConfirmationViewModel
from .warning_choices import WarningChoicesView, WarningChoicesViewModel
from .shell_command import ShellCommandView, ShellCommandViewModel
from .status_bar import StatusBarView
from .thinking_indicator import ThinkingIndicatorView, ThinkingIndicatorViewModel
from .tool_message import ToolMessageView, ToolMessageViewModel
from .view_model import ChatPaneViewModel
from rhizome.tui.types import ChatMessageData


FeedEntryWidget = Widget


class ChatPaneMVVM(ViewBase[ChatPaneViewModel]):

    BINDINGS = [
        Binding("shift+tab", "cycle_mode", "Cycle mode", show=False),
        Binding("ctrl+b", "cycle_verbosity", "Cycle verbosity", show=False, priority=True),
        # Commit-mode bindings. Guarded inside the action via ``check_action`` so they only fire
        # while ``state == COMMIT``. Priority handling:
        #   - up/down: priority so VerticalScroll's scroll bindings don't eat them when the
        #     message-area is focused. Trade-off: in commit mode, up/down in the chat input drive
        #     the cursor rather than history nav — acceptable since the input is just free-text
        #     instructions during commit.
        #   - space/enter/ctrl+j: no priority. When the input is focused it consumes them (typing /
        #     newline / submit-instructions); when the message-area is focused they bubble to the
        #     pane and toggle / submit.
        #   - ctrl+c / ctrl+up / ctrl+down: priority — always behave the same regardless of focus.
        Binding("up", "commit_cursor_up", "Commit: cursor up", show=False, priority=True),
        Binding("down", "commit_cursor_down", "Commit: cursor down", show=False, priority=True),
        Binding("space", "commit_toggle", "Commit: toggle", show=False),
        Binding("enter", "commit_toggle", "Commit: toggle", show=False),
        Binding("ctrl+j", "commit_submit", "Commit: submit", show=False),
        # ctrl+c dispatches by state: copy selection → exit commit (in COMMIT) → abandon turn
        # (CONVERSATION + current branch busy). Lives on the pane, not commit-prefixed, so it
        # bypasses ``check_action``'s commit-only gate.
        Binding("ctrl+c", "cancel", "Cancel", show=False, priority=False),
        Binding("ctrl+up", "commit_focus_cursor", "Commit: focus cursor", show=False, priority=True),
        Binding("ctrl+down", "commit_focus_input", "Commit: focus input", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    ChatPaneMVVM {
        layout: vertical;
        height: 1fr;
    }
    ChatPaneMVVM #message-area {
        height: 1fr;
        background: $surface-darken-1;
        padding: 1;
        scrollbar-color: rgb(60, 60, 60);
        scrollbar-color-hover: rgb(80, 80, 80);
        scrollbar-color-active: rgb(100, 100, 100);
    }
    ChatPaneMVVM #chat-input {
        height: auto;
        min-height: 3;
        max-height: 10;
        padding: 0 1;
        background: rgb(12, 12, 12);
    }
    ChatPaneMVVM #chat-input.--shell-mode,
    ChatPaneMVVM #chat-input.--shell-mode:focus {
        border: tall rgb(200, 60, 60);
    }
    ChatPaneMVVM CommandPalette {
        background: rgb(12, 12, 12);
    }
    """

    def __init__(self, *, session_factory=None, **kwargs) -> None:
        super().__init__(ChatPaneViewModel(session_factory=session_factory), **kwargs)

        # Mounted widgets keyed by FeedItem.id. The pane addresses widgets by id (not position)
        # because the feed may be mutated mid-stream — items can be appended after the agent's open
        # segment, and later in the refactor the router will remove items (e.g. the thinking
        # indicator) without disturbing surrounding positions.
        self._mounted: dict[int, FeedEntryWidget] = {}

        self._vm.subscribe(self._vm.feed_append, self._on_feed_append)
        self._vm.subscribe(self._vm.feed_remove, self._on_feed_remove)
        self._vm.subscribe(self._vm.feed_clear, self._on_feed_clear)
        self._vm.subscribe(self._vm.feed_replaced, self._on_feed_replaced)
        self._vm.subscribe(self._vm.notify, self._on_notify)

    def on_unmount(self) -> None:
        super().on_unmount()
        self._vm.unsubscribe(self._vm.feed_append, self._on_feed_append)
        self._vm.unsubscribe(self._vm.feed_remove, self._on_feed_remove)
        self._vm.unsubscribe(self._vm.feed_clear, self._on_feed_clear)
        self._vm.unsubscribe(self._vm.feed_replaced, self._on_feed_replaced)
        self._vm.unsubscribe(self._vm.notify, self._on_notify)

    def _on_notify(self, action: ChatPaneViewModel.NotifyAction) -> None:
        handler = self._NOTIFY_HANDLERS.get(action)
        if handler is None:
            return
        handler(self)

    def _notify_agent_busy(self) -> None:
        self.app.notify(
            "Agent is thinking, you can submit after it completes or interrupt with Ctrl+C"
        )

    def _notify_hint_higher_verbosity(self) -> None:
        self.app.notify(
            "Hint: the agent has indicated that a higher verbosity "
            "may be required to properly answer your query."
        )

    def _notify_descend_required(self) -> None:
        self.app.notify(
            "You're sitting on a branch point. Click a branch indicator and descend "
            "(ctrl+↓) into one of the branches to continue."
        )

    _NOTIFY_HANDLERS = {
        ChatPaneViewModel.NotifyAction.AGENT_BUSY: _notify_agent_busy,
        ChatPaneViewModel.NotifyAction.HINT_HIGHER_VERBOSITY: _notify_hint_higher_verbosity,
        ChatPaneViewModel.NotifyAction.DESCEND_REQUIRED: _notify_descend_required,
    }

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="message-area")
        yield ChatInputView(self._vm.chat_input, id="chat-input")
        yield CommandPalette(self._vm.command_palette, id="command-palette")
        yield StatusBarView(self._vm.status_bar, id="status-bar")

    def on_mount(self) -> None:
        self._vm.set_worker_scheduler(self.run_worker)
        self._vm.bootstrap_agent_session(
            self.app.options,  # type: ignore[attr-defined]
            debug=getattr(self.app, "debug_logging", False),
        )
        self.query_one("#chat-input", ChatInputView).focus()

    # ------------------------------------------------------------------
    # VM → view callbacks
    # ------------------------------------------------------------------

    def _build_entry_widget(self, entry) -> FeedEntryWidget:
        """Dispatch a feed entry's runtime type to its concrete view widget."""
        if isinstance(entry, ChatMessageData):
            return ChatMessageView(
                role=entry.role, content=entry.content, mode=entry.mode, rich=entry.rich,
            )
        if isinstance(entry, AgentMessageViewModel):
            return AgentMessageView(entry)
        if isinstance(entry, ToolMessageViewModel):
            return ToolMessageView(entry)
        if isinstance(entry, ThinkingIndicatorViewModel):
            return ThinkingIndicatorView(entry)
        if isinstance(entry, ShellCommandViewModel):
            return ShellCommandView(entry)
        if isinstance(entry, BranchIndicatorViewModel):
            return BranchIndicatorView(entry)
        if isinstance(entry, TestInterruptViewModel):
            return TestInterruptView(entry)
        if isinstance(entry, ChoicesViewModel):
            return ChoicesView(entry)
        if isinstance(entry, WarningChoicesViewModel):
            return WarningChoicesView(entry)
        if isinstance(entry, MultipleChoicesViewModel):
            return MultipleChoicesView(entry)
        if isinstance(entry, SqlConfirmationViewModel):
            return SqlConfirmationView(entry)
        if isinstance(entry, InterruptViewModelBase):
            raise TypeError(f"No view registered for interrupt type: {type(entry).__name__}")
        raise TypeError(f"Unhandled feed entry type: {type(entry).__name__}")

    def _on_feed_append(self, item_id: int) -> None:
        item = next((it for it in self._vm.visible_feed if it.id == item_id), None)
        if item is None:
            return
        area = self.query_one("#message-area", VerticalScroll)
        widget = self._build_entry_widget(item.entry)
        area.mount(widget)
        self._mounted[item_id] = widget
        area.scroll_end(animate=False)

    def _on_feed_remove(self, item_id: int) -> None:
        widget = self._mounted.pop(item_id, None)
        if widget is not None:
            widget.remove()

    def _on_feed_clear(self) -> None:
        for widget in self._mounted.values():
            widget.remove()

        self._mounted.clear()

    def _on_feed_replaced(self) -> None:
        """Reconcile the mounted widget set against ``self._vm.visible_feed`` after a cursor move.

        Diff is by ``FeedItem.id``: drop widgets whose ids aren't in the new visible feed, then
        mount any new ids in feed order. The structural guarantee that any two cursor paths share
        a common prefix means the delta is always a tail change, so plain ``area.mount(widget)``
        (which appends) preserves the correct visual order without ``before=``/``after=``.
        """
        new_ids = {item.id for item in self._vm.visible_feed}
        for stale_id in [mid for mid in self._mounted if mid not in new_ids]:
            self._mounted.pop(stale_id).remove()

        area = self.query_one("#message-area", VerticalScroll)
        for item in self._vm.visible_feed:
            if item.id in self._mounted:
                continue
            widget = self._build_entry_widget(item.entry)
            area.mount(widget)
            self._mounted[item.id] = widget
        area.scroll_end(animate=False)

    # ------------------------------------------------------------------
    # Compatibility shims for the --new-chat-pane integration. These let
    # MainScreen / ChatTabPane treat the MVVM widget like the legacy
    # ChatPane for the limited surface they use; remove once the swap is
    # permanent.
    # ------------------------------------------------------------------

    def append_message(self, msg) -> None:
        self._vm.append_message(msg)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def action_cycle_mode(self) -> None:
        await self._vm.cycle_mode()

    async def action_cycle_verbosity(self) -> None:
        await self._vm.cycle_verbosity()

    # ------------------------------------------------------------------
    # Commit-mode actions. ``check_action`` returns ``None`` to suppress the binding entirely when
    # the VM is not in COMMIT state, so up/down/enter etc. behave normally during conversations.
    # ------------------------------------------------------------------

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action.startswith("commit_"):
            if self._vm.state != ChatPaneViewModel.State.COMMIT:
                return None
            # When the chat input is focused, up/down should drive the TextArea / history nav, not
            # the commit cursor. Returning None suppresses the priority binding so the keystroke
            # falls through to the input's _on_key. Toggle / submit / cancel / focus-flip bindings
            # remain active regardless of focus.
            if action in ("commit_cursor_up", "commit_cursor_down"):
                if self.query_one("#chat-input", ChatInputView).has_focus:
                    return None
        return True

    def action_commit_cursor_up(self) -> None:
        self._vm.navigate_commit_cursor_up()

    def action_commit_cursor_down(self) -> None:
        self._vm.navigate_commit_cursor_down()

    def action_commit_toggle(self) -> None:
        self._vm.toggle_include_current_message_in_commit()

    def action_commit_submit(self) -> None:
        # ctrl+enter from the pane (input not focused) submits with empty instructions. ctrl+j sent
        # from the input is intercepted there as "insert newline" before this binding sees it.
        self._vm.submit_commit_payload("")

    def action_cancel(self) -> None:
        """ctrl+c dispatch — order matters:

        1. If there's selected text on screen, copy it (standard terminal behavior — most
           important for the user, do this first).
        2. In commit mode: exit commit mode.
        3. In conversation mode with the current branch's agent busy: cancel that turn.
        4. Otherwise: no-op.
        """
        selected = self.screen.get_selected_text()
        if selected:
            self.app.copy_to_clipboard(selected)
            return
        if self._vm.state == ChatPaneViewModel.State.COMMIT:
            self._vm.exit_commit_mode()
            return
        if self._vm.agent_busy:
            self._vm.cancel_agent_turn()

    def action_commit_focus_cursor(self) -> None:
        self._vm.request_focus()

    def action_commit_focus_input(self) -> None:
        self._vm.chat_input.request_focus()

    # The pane widget itself isn't focusable, so ``vm.request_focus()`` lands here — we route it to
    # the message-area scroll container. Keystrokes bubble back up to this pane, so the commit-mode
    # bindings still fire.
    def focus(self, scroll_visible: bool = True) -> "ChatPaneMVVM":
        self.query_one("#message-area", VerticalScroll).focus(scroll_visible=scroll_visible)
        return self
