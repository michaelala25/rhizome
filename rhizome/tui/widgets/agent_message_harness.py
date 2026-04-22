"""AgentMessageHarness — encapsulates one agent turn's display lifecycle."""

from __future__ import annotations

from typing import Any

from textual.message import Message
from textual.widget import Widget
from textual.widgets.markdown import Markdown, MarkdownStream

from langchain.messages import AIMessageChunk, ToolMessage

from rhizome.agent.context import AgentContext
from rhizome.agent.tools import TOOL_VISIBILITY, ToolVisibility
from rhizome.logs import get_logger
from rhizome.tui.types import Mode, Role

from .commit_proposal import CommitProposal
from .flashcard_proposal import FlashcardProposal
from .flashcard_review import FlashcardReview
from .interrupt import InterruptWidgetBase
from .choices import Choices
from .multiple_choices import MultipleChoices
from .sql_confirmation import SqlConfirmation
from .warning import WarningChoices
from .message import ChatMessage, MarkdownChatMessage
from .thinking import ThinkingIndicator
from .tool_call_list import ToolCallList

_logger = get_logger("tui.agent_message_harness")


class AgentMessageHarness(Widget):
    """Manages ThinkingIndicator → interleaved ChatMessage/ToolCallList segments for one agent turn."""

    _VISIBILITY_MAP: dict[str, ToolVisibility] = {
        "debug": ToolVisibility.LOW,
        "default": ToolVisibility.DEFAULT,
        "essential_only": ToolVisibility.HIGH,
    }

    DEFAULT_CSS = """
    AgentMessageHarness {
        height: auto;
        layout: vertical;
    }
    """

    def __init__(self, tool_use_visibility: str = "default", **kwargs) -> None:
        super().__init__(**kwargs)
        self._display_threshold = self._VISIBILITY_MAP.get(
            tool_use_visibility, ToolVisibility.DEFAULT
        )
        self._thinking: ThinkingIndicator | None = None
        self._segments: list[ChatMessage | ToolCallList | Widget] = []
        self._active_stream: MarkdownStream | None = None
        self._interrupt_widget: InterruptWidgetBase | None = None
        self._finalized: bool = False

    @property
    def _session_mode(self) -> Mode:
        # Needed to avoid circular import
        from .chat_pane import ChatPane

        pane = self.query_ancestor(ChatPane)
        return pane.session_mode

    @property
    def chat_message_body(self) -> str | None:
        """Concatenated body text from all ChatMessage segments."""
        bodies = [seg._body for seg in self._segments if isinstance(seg, ChatMessage)]
        return "".join(bodies) if bodies else None

    @property
    def is_thinking(self) -> bool:
        return self._thinking is not None

    def on_mount(self) -> None:
        self.set_interval(0.2, self._sync_session_mode)

    def _sync_session_mode(self) -> None:
        # Only respond to changes in the session mode _before_ we've finalized
        # the message. This way, changes to the mode won't change the background
        # colour of past messages.
        if not self._finalized:
            mode = self._session_mode
            self.set_class(mode == Mode.LEARN, "learn-mode")
            self.set_class(mode == Mode.REVIEW, "review-mode")
            self.set_class(mode == Mode.IDLE, "idle-mode")

            # Apply mode classes to the current (last) ToolCallList only;
            # prior ToolCallList segments keep the mode they were created with.
            last_tl = self._last_tool_list
            if last_tl is not None:
                last_tl.set_class(mode == Mode.LEARN, "learn-mode")
                last_tl.set_class(mode == Mode.REVIEW, "review-mode")
                last_tl.set_class(mode == Mode.IDLE, "idle-mode")

    @property
    def _last_tool_list(self) -> ToolCallList | None:
        """Return the last ToolCallList segment, or None."""
        for seg in reversed(self._segments):
            if isinstance(seg, ToolCallList):
                return seg
        return None

    async def start_thinking(self) -> None:
        """Mount a ThinkingIndicator inside this harness."""
        # Remove shortcut hints from all previous messages in the message area
        parent = self.parent
        if parent is not None:
            for msg in parent.query("ChatMessage.--show-shortcut"):
                msg.remove_class("--show-shortcut")
                msg._update_collapse_label()
            for tl in parent.query("ToolCallList.--show-hint"):
                tl.remove_class("--show-hint")
                tl._update_title()
        self._thinking = ThinkingIndicator()
        await self.mount(self._thinking)

    async def stop_thinking(self) -> None:
        """Idempotently remove the ThinkingIndicator."""
        if self._thinking is not None:
            await self._thinking.remove()
            self._thinking = None

    async def append(self, token: AIMessageChunk) -> None:
        """Append a text token to the streaming message.

        Creates a new ChatMessage segment if the last segment is not a ChatMessage
        (or if no segments exist yet).
        """
        if self._finalized:
            raise Exception  # TODO: raise a proper exception

        # Agents will produce AIMessageChunks of type "input_json_delta" when constructing
        # args for tool calls, which have empty text. We want to ignore these until the
        # agent produces an actual text token as part of it's message, so we don't initialize
        # the chat message too early.
        if not token.text:
            return

        # If the last segment isn't a ChatMessage, start a new one.
        if not self._segments or not isinstance(self._segments[-1], ChatMessage):
            await self._start_chat_segment()

        chat = self._segments[-1]
        assert isinstance(chat, ChatMessage)

        chat._body += token.text
        if not chat._collapsed and self._active_stream:
            await self._active_stream.write(token.text)

    async def _mount_segment(self, widget: Widget) -> None:
        """Mount a segment, keeping the ThinkingIndicator at the bottom."""
        if self._thinking is not None:
            await self.mount(widget, before=self._thinking)
        else:
            await self.mount(widget)

    async def _start_chat_segment(self) -> None:
        """Create and mount a new ChatMessage segment, opening a fresh stream."""
        chat = MarkdownChatMessage(role=Role.AGENT, mode=self._session_mode)
        self._segments.append(chat)
        await self._mount_segment(chat)
        self._active_stream = Markdown.get_stream(chat.inner_markdown)

    async def post_update(self, chunk: dict) -> None:
        """Handle a graph state update. Extracts tool call names from AIMessage content.

        AIMessage content blocks come in several forms we need to handle:
        - ``"tool_use"`` — local tool calls (our @tool-decorated functions).
        - ``"server_tool_use"`` — Anthropic server-side tools (web_search, web_fetch).
          Anthropic wraps some server tools in a ``code_execution`` block that
          internally calls the real tool; we skip those to avoid duplicates.
        - Other block types (``"text"``, ``"server_tool_result"``, etc.) are ignored.
        """
        for update in chunk.values():
            if update is None:
                continue
            for msg in update.get("messages", []):
                content = getattr(msg, "content", None)
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    # Skip Anthropic's internal code_execution wrapper — the
                    # actual tool (web_fetch etc.) appears as its own block.
                    if btype == "server_tool_use" and block.get("name") == "code_execution":
                        continue
                    if btype not in ("tool_use", "server_tool_use"):
                        continue
                    _logger.debug("%s block: %r", btype, block)
                    name = block.get("name")
                    if not name:
                        continue
                    level = TOOL_VISIBILITY.get(name, ToolVisibility.DEFAULT)
                    if level < self._display_threshold:
                        continue
                    # If the last segment isn't a ToolCallList, close the
                    # active stream and start a new tool list segment.
                    if not self._segments or not isinstance(self._segments[-1], ToolCallList):
                        await self._close_active_stream()
                        tool_list = ToolCallList(classes=f"{self._session_mode.value}-mode")
                        self._segments.append(tool_list)
                        await self._mount_segment(tool_list)
                    last = self._segments[-1]
                    assert isinstance(last, ToolCallList)
                    args = block.get("input") or {}
                    if not args and block.get("partial_json"):
                        import json
                        try:
                            args = json.loads(block["partial_json"])
                        except (json.JSONDecodeError, TypeError):
                            pass
                    last.add_tool(name, args)

    async def _close_active_stream(self) -> None:
        """Stop the active MarkdownStream if one is open."""
        if self._active_stream is not None:
            await self._active_stream.stop()
            self._active_stream = None

    # ------------------------------------------------------------------
    # Textual messages for interrupt coordination
    # ------------------------------------------------------------------

    class InterruptPending(Message):
        """Posted when an interrupt widget is mounted and needs user input."""

        def __init__(self, widget: Widget) -> None:
            super().__init__()
            self.widget = widget

    class InterruptResolved(Message):
        """Posted when the user has resolved an interrupt."""

    # ------------------------------------------------------------------
    # Callback methods for AgentSession.stream()
    # ------------------------------------------------------------------

    async def on_message(self, kind: str, payload: Any) -> None:
        """Callback for ``"messages"`` chunks from the agent stream."""
        chunk, _metadata = payload
        # Only process AIMessageChunks — filter out ToolMessages, HumanMessages
        # (e.g. [System] notifications injected by middleware), and anything
        # else that isn't an AI token.
        if not isinstance(chunk, AIMessageChunk):
            return
        await self.append(chunk)

    async def on_update(self, kind: str, payload: Any) -> None:
        """Callback for ``"updates"`` chunks from the agent stream."""
        await self.post_update(payload)

    async def on_interrupt(self, interrupt_value: Any, context: AgentContext) -> Any:
        """Callback for graph interrupts. Blocks until the user responds.

        Dispatches to the appropriate interrupt widget based on the ``"type"``
        key in the interrupt value dict, using the ``INTERRUPT_REGISTRY``.
        """
        await self.stop_thinking()
        await self._close_active_stream()

        if not isinstance(interrupt_value, dict):
            raise ValueError(
                f"Interrupt value must be a dict with a 'type' key, got {type(interrupt_value).__name__}"
            )

        itype = interrupt_value["type"]
        if itype == "choices":
            widget = Choices.from_interrupt(interrupt_value)
        elif itype == "warning":
            widget = WarningChoices.from_interrupt(interrupt_value)
        elif itype == "multiple_choice":
            widget = MultipleChoices.from_interrupt(interrupt_value)
        elif itype == "commit_proposal":
            widget = CommitProposal.from_interrupt(interrupt_value)
        elif itype == "flashcard_proposal":
            widget = FlashcardProposal.from_interrupt(interrupt_value)
        elif itype == "sql_confirmation":
            widget = SqlConfirmation.from_interrupt(interrupt_value)
        elif itype == "flashcard_review":
            widget = FlashcardReview.from_interrupt(interrupt_value)
        else:
            raise ValueError(f"Unknown interrupt type: {itype!r}")
        self._interrupt_widget = widget
        self._segments.append(widget)
        await self._mount_segment(widget)

        # Tell ChatPane to disable its input and focus the choices widget
        self.post_message(self.InterruptPending(widget=self._interrupt_widget))

        try:
            result = await self._interrupt_widget.wait_for_selection()
        finally:
            self._interrupt_widget = None
            # Tell ChatPane to re-enable input
            self.post_message(self.InterruptResolved())
            await self.start_thinking()

        return result

    async def finalize(self) -> str:
        """Stop the stream and finalize the message. Returns accumulated message body."""
        # Remark: The (no response) message is posted if no ChatMessage segments exist,
        # meaning the agent never said anything.
        return await self._finalize(empty_chat_message="(no response)")

    async def cancel(self) -> str:
        """Cancel the current turn. Returns accumulated body (may be empty)."""
        # Clean up any pending interrupt widget
        if self._interrupt_widget is not None:
            self._interrupt_widget.cancel()
            self._interrupt_widget = None
        return await self._finalize(empty_chat_message="*(cancelled)*")

    async def _finalize(self, empty_chat_message: str | None = None) -> str:
        # Remove the thinking indicator if present
        await self.stop_thinking()

        # Close the stream if opened
        await self._close_active_stream()

        has_chat = any(isinstance(seg, ChatMessage) for seg in self._segments)

        if not has_chat:
            if empty_chat_message:
                chat = MarkdownChatMessage(role=Role.AGENT, content=empty_chat_message)
                self._segments.append(chat)
                await self.mount(chat)
            else:
                self._finalized = True
                return ""

        self._finalized = True
        # Notify each ChatMessage segment so it can update collapsibility.
        for seg in self._segments:
            if isinstance(seg, ChatMessage):
                seg.update_body(seg._body)
        # Show shortcut hint on the last ChatMessage segment (only if collapsible)
        chat_segments = [seg for seg in self._segments if isinstance(seg, ChatMessage)]
        if chat_segments and chat_segments[-1].has_class("--collapsible"):
            chat_segments[-1].add_class("--show-shortcut")
            chat_segments[-1]._update_collapse_label()
        # Show hint on the last ToolCallList segment
        tool_segments = [seg for seg in self._segments if isinstance(seg, ToolCallList)]
        if tool_segments:
            tool_segments[-1].add_class("--show-hint")
            tool_segments[-1]._update_title()
        # Join bodies from all ChatMessage segments
        return "".join(
            seg._body for seg in self._segments if isinstance(seg, ChatMessage)
        )
