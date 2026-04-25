"""ChatPane — core chat UI: message area, input box, and command palette."""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import time
import traceback
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Literal

from langchain_core.messages import HumanMessage
from langchain_core.messages.utils import count_tokens_approximately

import rich_click as click

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static, TabbedContent
from textual.worker import Worker

from langchain.chat_models import init_chat_model

from rhizome.agent import AgentSession
from rhizome.agent.config import get_api_key
from rhizome.agent.session import get_agent_kwargs
from rhizome.db import Topic
from rhizome.db.operations import (
    delete_resource,
    get_resource,
    insert_sections,
    link_chunks_to_sections,
    link_resource_to_topic,
    resolve_resource,
    resolve_topic,
    unlink_resource_from_topic,
)
from rhizome.logs import get_logger
from rhizome.resources import ResourceManager
from rhizome.resources.auto_metadata import generate_resource_metadata
from rhizome.resources.ingest import extract_text_from_file, fetch_webpage_text, ingest_resource
from rhizome.tui.commit_state import CommitApproved, CommitState
from rhizome.tui.commands import CommandRegistry, parse_input
from rhizome.tui.options import Options, OptionScope, build_jsonc_snapshot, parse_jsonc
from rhizome.tui.dock import DockContainerMixin, HorizontalDockArea, VerticalDockArea
from rhizome.tui.types import ChatMessageData, DatabaseCommitted, Mode, Role

from .chat_input import ChatInput
from .command_palette import CommandPalette
from .agent_message_harness import AgentMessageHarness
from .shell_message import ShellCommandMessage
from .message import ChatMessage, MarkdownChatMessage, RichChatMessage
from .options_editor import OptionsEditor
from .welcome import WelcomeHeader
from .status_bar import StatusBar
from .explorer_viewer import ExplorerViewer
from .messages import ActiveTopicChanged
from .resource.view_model import ResourceViewerViewModel
from .resource.viewer import ResourceViewer
from .commit_proposal import CommitProposal
from .flashcard_proposal import FlashcardProposal
from .flashcard_review.view import FlashcardReview
from .choices import Choices
from .multiple_choices import MultipleChoices
from .navigable import WidgetDeactivated
from .resource.loader_tree import _fmt_tokens
from .thinking import Spinner


class HintHigherVerbosity(Message):
    """Posted by the hint_higher_verbosity tool to suggest the user raise verbosity."""


@dataclass
class _AmbiguousSpec:
    """One ambiguous identifier awaiting user disambiguation."""
    key: str        # unique key to identify this in the results dict
    prompt: str     # e.g. "Multiple topics match 'Types':"
    options: list[str]  # e.g. ["[14] Linux > ... > Types", "[31] Programming > ... > Types"]


class ChatPane(Widget, DockContainerMixin):
    """Reusable chat pane containing the message area, input, and command palette."""

    # ------------------------------------------------------------------
    # Class-level constants
    # ------------------------------------------------------------------

    BINDINGS = [
        ("ctrl+c", "cancel_or_copy", "Cancel/Copy"),
        Binding("ctrl+l", "refocus_input", "Refocus input", show=False, priority=True),
        Binding("ctrl+t", "toggle_last_agent_message", "Toggle agent msg", show=False, priority=True),
        Binding("ctrl+o", "toggle_last_tool_call", "Toggle tool call", show=False, priority=True),
        Binding("shift+tab", "cycle_mode", "Cycle mode", show=False, priority=True),
        Binding("ctrl+b", "cycle_verbosity", "Cycle verbosity", show=False, priority=True),
        Binding("ctrl+up", "focus_prev_widget", "Prev widget", show=False, priority=True),
        Binding("ctrl+down", "focus_next_widget", "Next widget", show=False, priority=True),
        Binding("ctrl+r", "refocus_resources", "Refocus resources", show=False, priority=True),
        Binding("ctrl+d", "cycle_dock_position", "Cycle dock", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    ChatPane {
        layout: horizontal;
        background: rgb(12, 12, 12);
    }

    /* -- Dock areas --------------------------------------------------- */
    #dock-left {
        width: 25%;
        height: 1fr;
        border-right: solid rgb(60, 60, 60);
    }
    #dock-right {
        width: 25%;
        height: 1fr;
        border-left: solid rgb(60, 60, 60);
    }
    #dock-center-col {
        width: 1fr;
        height: 1fr;
    }
    #dock-bottom {
        height: auto;
        max-height: 50%;
        border-top: solid rgb(60, 60, 60);
    }
    .--dock-empty {
        display: none;
    }

    /* -- Chat content (the old grid, now inside #chat-content) -------- */
    #chat-content {
        layout: grid;
        grid-size: 1;
        grid-rows: 1fr auto auto auto;
        height: 1fr;
    }
    #status-bar {
        height: auto;
        background: rgb(12, 12, 12);
        padding: 0 1 1 1;
        border-top: solid rgb(60, 60, 60);
    }
    #message-area {
        background: $surface-darken-1;
        padding: 1;
        scrollbar-color: rgb(60, 60, 60);
        scrollbar-color-hover: rgb(80, 80, 80);
        scrollbar-color-active: rgb(100, 100, 100);
    }
    #chat-input {
        height: auto;
        min-height: 3;
        max-height: 10;
        padding: 0 1;
        background: rgb(12, 12, 12);
    }
    #chat-input.--shell-mode,
    #chat-input.--shell-mode:focus {
        border: tall rgb(200, 60, 60);
    }
    #commit-instructions {
        height: auto;
        min-height: 3;
        max-height: 10;
        padding: 0 1;
        background: rgb(12, 12, 12);
        display: none;
    }
    #command-palette {
        background: rgb(12, 12, 12);
    }
    """

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __init__(self, *, session_factory=None, show_welcome: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self._session_factory = session_factory

        # Whether to show the welcome header on mount, or start with an empty message area.
        self._show_welcome = show_welcome

        # The list of all messages in this chat pane, including those from the agent stream and user/system messages.
        # This is the "view" level representation of the messages, separate from the conversation history managed by
        # the agent session.
        #
        # TODO: do we even really need this?
        self.messages: list[ChatMessageData] = []

        self.session_mode: Mode = Mode.IDLE
        self.options: Options | None = None  # set on mount when app is available

        # Active topic and path, if any. _topic_path is the list of topic names from the root to the active topic,
        # used for display in the status bar.
        self.active_topic: Topic | None = None
        self._topic_path: list[str] = []

        # Agent session and worker state.
        # - _agent_session is the AgentSession instance for this chat pane, which manages the conversation history and agent stream.
        # - _agent_busy is True from the moment an agent turn is initiated until its worker completes.
        # - _agent_worker holds the current agent worker, if any, so it can be cancelled if the user interrupts.
        self._agent_session: AgentSession | None = None
        self._agent_busy: bool = False
        self._agent_worker: Worker[None] | None = None
        # Commit mode state — see CommitState dataclass.
        self._commit = CommitState()
        # Resource manager — shared between agent session and resource viewer.
        self._resource_manager = ResourceManager(session_factory=session_factory)
        # Resource viewer view model — outlives the widget across dock changes.
        self._resource_viewer_vm = ResourceViewerViewModel()
        self._resource_viewer: ResourceViewer | None = None
        self._resource_viewer_dock_id: str = "dock-bottom"

        self._log = get_logger("tui.chat_pane")

        # Active widget stack — interactive widgets in mount order.
        # Ctrl+Up/Down navigates between them.
        self._active_widgets: list[Widget] = []

        # Command registry
        self._command_registry = CommandRegistry()
        self._register_commands(self._command_registry)

        # Verbosity-hint throttling: show the toast only on the first hint
        # since the last answer-verbosity change, or if 10+ minutes have passed.
        self._verbosity_hint_allowed = True
        self._verbosity_hint_last_shown: float = 0.0

    def compose(self) -> ComposeResult:
        yield HorizontalDockArea(id="dock-left")
        with Vertical(id="dock-center-col"):
            with Vertical(id="chat-content"):
                yield VerticalScroll(id="message-area")
                yield ChatInput(placeholder="Type a message or /command ...", id="chat-input")
                yield ChatInput(placeholder="Add instructions for the commit (Enter to skip)...", id="commit-instructions")
                yield CommandPalette(id="command-palette")
            yield VerticalDockArea(id="dock-bottom")
            yield StatusBar(id="status-bar")
        yield HorizontalDockArea(id="dock-right")

    def on_mount(self) -> None:
        self._sync_registry_width()

        # Construct the per-session options object
        self.options = Options(
            scope=OptionScope.Session,
            parent=self.app.options # type: ignore[attr-defined]
        )

        # Create the agent with initial provider/model from options
        provider = self.options.get(Options.Agent.Provider)
        model_name = self.options.get(Options.Agent.Model)
        agent_kwargs = get_agent_kwargs(self.options)
        self._agent_session = AgentSession(
            self._session_factory,
            chat_pane=self,
            resource_manager=self._resource_manager,
            provider=provider,
            model_name=model_name,
            agent_kwargs=agent_kwargs,
            on_token_usage_changed=self.update_status_bar,
            on_rebuild_agent=self._on_agent_rebuilt,
            debug=getattr(self.app, "debug_logging", False),
        )
        self.update_status_bar() # Call to trigger model name display

        # Subscribe to post-update so agent rebuilds when options change
        self.options.subscribe_post_update(self._agent_session.on_options_post_update)
        self.options.subscribe_post_update(self._on_options_post_update)

        # Add the welcome header, assuming _show_welcome is True.
        area = self.query_one("#message-area", VerticalScroll)
        if self._show_welcome:
            area.mount(WelcomeHeader())

        # Focus the chat input
        self.query_one("#chat-input", ChatInput).focus()

    def on_unmount(self) -> None:
        if self.options is not None:
            self.options.detach()

    def on_resize(self) -> None:
        self._sync_registry_width()

    def _sync_registry_width(self) -> None:
        """Update the command registry's max_content_width from the chat content area's width."""
        try:
            width = self.query_one("#chat-content", Vertical).size.width
        except Exception:
            width = self.size.width
        self._command_registry.max_content_width = max(width - 15, 40)

    # ------------------------------------------------------------------
    # Dock area management
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # Agent session
    # ------------------------------------------------------------------

    def _start_agent(self):
        # Create a harness, and run the agent. This sets _agent_busy = True internally
        # as well, until the worker finishes or is cancelled.
        harness = AgentMessageHarness(
            tool_use_visibility=self.options.get(Options.ToolUseVisibility),
        )
        self._agent_worker = self.run_worker(partial(self._run_agent, harness))

    async def _run_agent(self, harness: AgentMessageHarness) -> None:
        message_area = self.query_one("#message-area", VerticalScroll)
        message_area.mount(harness)
        message_area.scroll_end(animate=False)

        try:
            assert self._agent_session is not None
            assert not self._agent_busy

            self._agent_busy = True
            await harness.start_thinking()
            message_area.scroll_end(animate=False)

            await self._agent_session.stream(
                mode=self.session_mode.value,
                topic_name=self.active_topic.name if self.active_topic else "",
                on_message=harness.on_message,
                on_update=harness.on_update,
                on_interrupt=harness.on_interrupt,
                post_chunk_handler=lambda: message_area.scroll_end(animate=False),
            )
            
            body = await harness.finalize()
            if body:
                self.messages.append(ChatMessageData(role=Role.AGENT, content=body))

        except asyncio.CancelledError:
            self._log.info("User cancelled agent stream.")
            body = await harness.cancel()
            if body:
                self.messages.append(ChatMessageData(role=Role.AGENT, content=body))

                # Remark: here we re-inject whatever the agent was able to produce before it was interrupted back into the message
                # queue as a fake AI message. When asyncio.CancelledError is triggered it seems like the half-finished message doesn't
                # actually make it into the graph's messages state - manually queueing the partial message provides it as additional 
                # context for the agent.
                self._agent_session.add_ai_message(body)

            self.append_message(
                ChatMessageData(role=Role.SYSTEM, content="(user cancelled)")
            )

        except Exception as exc:
            self._log.error("Agent error: %s", exc)
            await harness.cancel()
            self.append_message(ChatMessageData(role=Role.ERROR, content=str(exc)))
            # raise

        finally:
            self._agent_busy = False
            self._agent_worker = None

    def _on_agent_rebuilt(self, old_model: str, new_model: str) -> None:
        """Called when the agent is rebuilt due to a model option change."""
        if new_model == old_model:
            return

        self._log.info("Agent rebuilt: %s → %s", old_model, new_model)
        self.append_message(ChatMessageData(
            role=Role.SYSTEM,
            content=(
                f"Model changed to {self._agent_session._model_name}.  \n"
                f"Profile: `{self._agent_session.model.profile}`"
            ),
        ))
        self.update_status_bar() # Model name changed.

    def on_agent_message_harness_interrupt_pending(
        self, event: AgentMessageHarness.InterruptPending
    ) -> None:
        """Disable chat input while an interrupt widget awaits user input."""
        chat_input = self.query_one("#chat-input", ChatInput)
        chat_input.disabled = True
        chat_input.placeholder = "Respond to the agent's prompt above..."
        self._active_widgets.append(event.widget)
        event.widget.focus()

    def on_agent_message_harness_interrupt_resolved(
        self, event: AgentMessageHarness.InterruptResolved
    ) -> None:
        """Re-enable chat input after the user resolves an interrupt."""
        self._restore_chat_input()

    def on_widget_deactivated(self, event: WidgetDeactivated) -> None:
        """Remove a widget from the active stack when it is no longer interactable."""
        sender = event.sender_widget
        if sender in self._active_widgets:
            self._active_widgets.remove(sender)

    # ------------------------------------------------------------------
    # Message area & status bar
    # ------------------------------------------------------------------

    def append_message(self, msg: ChatMessageData, ui_only=False) -> None:
        """Append a message to the history and mount its widget."""
        # Deduplicate consecutive identical system messages by pinging the existing one.
        if msg.role == Role.SYSTEM:
            area = self.query_one("#message-area", VerticalScroll)
            children = area.children
            if children and isinstance(children[-1], ChatMessage) and children[-1]._role == Role.SYSTEM and children[-1]._body == msg.content:
                children[-1].ping()
                return

        msg.mode = self.session_mode
        self.messages.append(msg)

        # Add message to agent message history.
        if not ui_only and self._agent_session is not None:
            if msg.role == Role.USER:
                self._agent_session.add_human_message(msg.content)
            elif msg.role == Role.SYSTEM:
                self._agent_session.add_system_notification(msg.content)

        area = self.query_one("#message-area", VerticalScroll)
        if msg.rich:
            widget = RichChatMessage(role=msg.role, content=msg.content, mode=msg.mode)
        else:
            widget = MarkdownChatMessage(role=msg.role, content=msg.content, mode=msg.mode)

        # Remark: this part identifies if the current message and the previous message are both system/error messages, and if so
        # adds the --after-system class to the current message. This allows us to style consecutive system/error messages differently
        if msg.role in (Role.SYSTEM, Role.ERROR):
            children = area.children
            if children and isinstance(children[-1], ChatMessage) and children[-1]._role in (Role.SYSTEM, Role.ERROR):
                widget.add_class("--after-system")

        area.mount(widget)
        area.scroll_end(animate=False)

    def update_status_bar(self) -> None:
        """Sync the status bar with the current mode and context."""
        bar = self.query_one("#status-bar", StatusBar)
        bar.mode = self.session_mode.value
        bar.topic_path = list(self._topic_path)
        if self._agent_session is not None:
            bar.token_usage = self._agent_session.token_usage
            bar.model_name = self._agent_session._model_name or ""
        if self.options is not None:
            bar.verbosity = self.options.get(Options.Agent.AnswerVerbosity)
        bar.mutate_reactive(StatusBar.token_usage)

    def _show_commit_instructions(self) -> None:
        """Hide the main chat input and show the commit instructions input."""
        self.query_one("#chat-input").styles.display = "none"
        instructions = self.query_one("#commit-instructions", ChatInput)
        instructions.submit_empty = True
        instructions.suppress_history = True
        instructions.styles.display = "block"
        instructions.focus()

    def _dismiss_commit_instructions(self) -> None:
        """Hide the commit instructions input and restore the main chat input."""
        instructions = self.query_one("#commit-instructions", ChatInput)
        instructions.styles.display = "none"
        instructions.clear()
        chat_input = self.query_one("#chat-input", ChatInput)
        chat_input.styles.display = "block"
        chat_input.disabled = False
        chat_input.placeholder = "Type a message or /command ..."
        chat_input.focus()

    def _restore_chat_input(self) -> None:
        """Restore the main chat input to its default state."""
        chat_input = self.query_one("#chat-input", ChatInput)
        chat_input.disabled = False
        chat_input.placeholder = "Type a message or /command ..."
        chat_input.focus()

    # ------------------------------------------------------------------
    # Input handling & command palette
    # ------------------------------------------------------------------

    # Commands that require the agent to be idle before executing.
    _AGENT_GATED_COMMANDS = {"commit", "options"}

    def on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        # Commit instructions input — swap back and submit the commit.
        if event.input.id == "commit-instructions":
            self._dismiss_commit_instructions()
            self._submit_commit(event.value.strip())
            return

        self._hide_palette()

        text = event.value.strip()
        if not text:
            return

        # Shell commands (!cmd) — ungated, can run while agent is busy.
        if text.startswith("!"):
            chat_input = self.query_one("#chat-input", ChatInput)
            chat_input.clear()
            chat_input.push_history(text)
            shell_cmd = text[1:].strip()
            if shell_cmd:
                self._handle_shell_command(shell_cmd)
            return

        command = parse_input(text)
        needs_idle = command is None or command.name in self._AGENT_GATED_COMMANDS

        if needs_idle and self._agent_busy:
            self.notify("Agent is thinking, you can submit after it completes or interrupt with Ctrl+C")
            return

        chat_input = self.query_one("#chat-input", ChatInput)
        chat_input.clear()
        chat_input.push_history(text)

        if command is not None:
            self._handle_command(command.name, command.args)
        else:
            self._handle_chat(text)

    def _handle_command(self, name: str, args: str) -> None:
        self._log.debug("command dispatched: /%s %s", name, args)

        async def _run() -> None:
            try:
                line = f"{name} {args}".strip()
                result = await self._command_registry.execute(line)
                if result:
                    self.append_message(
                        ChatMessageData(role=Role.SYSTEM, content=result, rich=True)
                    )
            except KeyError:
                self.notify(f"Unknown command: /{name}", severity="error")
            except Exception as e:
                self._log.exception("Error executing command: /%s %s", name, args, exc_info=e, stack_info=True)
                self.append_message(
                    ChatMessageData(
                        role=Role.ERROR,
                        content=(
                            f"Error executing command /{name} {args}: {traceback.format_exc()}"
                        )
                    )
                )

        self.run_worker(_run())

    def _handle_shell_command(self, cmd: str) -> None:
        """Run a shell command and display output in a ShellCommandMessage widget."""
        self._log.debug("shell command dispatched: !%s", cmd)
        area = self.query_one("#message-area", VerticalScroll)
        widget = ShellCommandMessage(cmd)
        area.mount(widget)
        area.scroll_end(animate=False)

        async def _run() -> None:
            await widget.execute()
            area.scroll_end(animate=False)

        self.run_worker(_run())

    def _handle_chat(self, text: str) -> None:
        self._log.debug("Chat submitted (%d chars)", len(text))

        # Post the user's message to the message history
        self.append_message(ChatMessageData(role=Role.USER, content=text))

        # Start the agent to have it respond
        self._start_agent()

    def on_text_area_changed(self, event: ChatInput.Changed) -> None:
        if event.text_area.id != "chat-input":
            return

        text = event.text_area.text

        palette = self.query_one("#command-palette", CommandPalette)
        chat_input = self.query_one("#chat-input", ChatInput)

        # Toggle shell-mode border color when input starts with '!'
        if text.startswith("!"):
            chat_input.add_class("--shell-mode")
        else:
            chat_input.remove_class("--shell-mode")

        if chat_input._history_index >= 0:
            palette.remove_class("visible")
            chat_input.palette_active = False
            return

        if text.startswith("/") and "\n" not in text:
            palette.filter_text = text
            if palette.has_items:
                palette.add_class("visible")
                chat_input.palette_active = True
            else:
                palette.remove_class("visible")
                chat_input.palette_active = False
        else:
            palette.remove_class("visible")
            chat_input.palette_active = False

    def on_chat_input_palette_navigate(self, event: ChatInput.PaletteNavigate) -> None:
        self.query_one("#command-palette", CommandPalette).move_selection(event.delta)

    def on_chat_input_palette_confirm(self, event: ChatInput.PaletteConfirm) -> None:
        self.query_one("#command-palette", CommandPalette).confirm_selection()

    def on_command_palette_command_selected(self, event: CommandPalette.CommandSelected) -> None:
        chat_input = self.query_one("#chat-input", ChatInput)
        chat_input.clear()
        chat_input.insert(f"/{event.name} ")
        self._hide_palette()

    def _hide_palette(self) -> None:
        """Hide the command palette and restore input margin."""
        palette = self.query_one("#command-palette", CommandPalette)
        chat_input = self.query_one("#chat-input", ChatInput)
        palette.remove_class("visible")
        chat_input.remove_class("palette-open")
        chat_input.palette_active = False

    # ------------------------------------------------------------------
    # Keybinding actions
    # ------------------------------------------------------------------

    def cancel_agent(self) -> None:
        """Cancel the running agent worker, if any."""
        if self._agent_busy and self._agent_worker is not None:
            self._agent_worker.cancel()

    def action_cancel_or_copy(self) -> None:
        selected = self.screen.get_selected_text()
        if selected:
            self.app.copy_to_clipboard(selected)
        else:
            self.cancel_agent()

    def action_refocus_input(self) -> None:
        self.query_one("#chat-input").focus()

    def action_focus_prev_widget(self) -> None:
        self._navigate_active_widgets(-1)

    def action_focus_next_widget(self) -> None:
        self._navigate_active_widgets(1)

    def _navigate_active_widgets(self, direction: int) -> None:
        """Navigate the active widget stack by *direction* (-1 or +1)."""
        if not self._active_widgets:
            return

        # Find which widget currently has focus (or contains focus).
        focused = self.screen.focused
        current_idx: int | None = None
        if focused is not None:
            for i, w in enumerate(self._active_widgets):
                if focused is w or w in focused.ancestors_with_self:
                    current_idx = i
                    break

        if current_idx is not None:
            new_idx = current_idx + direction
            # Clamp — don't wrap, just stop at the ends.
            new_idx = max(0, min(new_idx, len(self._active_widgets) - 1))
        else:
            # Focus is not on any active widget — jump to nearest end.
            new_idx = len(self._active_widgets) - 1 if direction < 0 else 0

        target = self._active_widgets[new_idx]
        target.focus()
        target.scroll_visible(animate=False)

    def action_toggle_last_agent_message(self) -> None:
        harnesses = self.query(AgentMessageHarness)
        for harness in reversed(harnesses):
            msgs = harness.query(ChatMessage)
            if msgs:
                last_msg = list(msgs)[-1]
                last_msg.toggle_collapse()
                return

    def action_toggle_last_tool_call(self) -> None:
        harnesses = self.query(AgentMessageHarness)
        for harness in reversed(harnesses):
            tool_list = harness._last_tool_list
            if tool_list is not None:
                tool_list.action_toggle_collapse()
                return

    async def action_cycle_mode(self) -> None:
        cycle = {Mode.IDLE: Mode.LEARN, Mode.LEARN: Mode.REVIEW, Mode.REVIEW: Mode.IDLE}
        await self._set_mode(cycle[self.session_mode], silent=True)

    async def action_cycle_verbosity(self) -> None:
        choices = Options.Agent.AnswerVerbosity.choices
        current = self.options.get(Options.Agent.AnswerVerbosity)
        idx = choices.index(current) if current in choices else 0
        new_value = choices[(idx + 1) % len(choices)]
        await self.options.set(Options.Agent.AnswerVerbosity, new_value)
        await self.options.post_update()
        self.update_status_bar()

    async def _on_options_post_update(self, options: Options) -> None:
        """React to option changes: reset verbosity hint, clear stale cache display."""
        self._verbosity_hint_allowed = True

        if (
            options.get(Options.Agent.Provider) == "anthropic"
            and options.get(Options.Agent.Anthropic.PromptCache) != "enabled"
        ):
            if self._agent_session is not None:
                self._agent_session.token_usage.cache_read_tokens = None
                self._agent_session.token_usage.cache_creation_tokens = None
                self.update_status_bar()

    # ------------------------------------------------------------------
    # Command registration & handlers
    # ------------------------------------------------------------------

    def _register_commands(self, registry: CommandRegistry) -> None:
        """Register all slash commands with the click-based registry."""

        @registry.group(name="options", help="Open settings and configuration",
                        invoke_without_command=True)
        @click.option("-e", "--edit", is_flag=True, help="Open in $EDITOR")
        @click.option("-g", "--global", "scope", flag_value="global",
                      help="Target global options")
        @click.option("-s", "--session", "scope", flag_value="session",
                      default=True, help="Target session options (default)")
        @click.pass_context
        async def options_group(ctx, edit, scope):
            if ctx.invoked_subcommand is None:
                await self._cmd_options(edit=edit, scope=scope)

        @options_group.command(name="get", help="Get an option value")
        @click.option("-g", "--global", "scope", flag_value="global",
                      help="Target global options")
        @click.option("-s", "--session", "scope", flag_value="session",
                      default=True, help="Target session options (default)")
        @click.argument("name")
        async def options_get(scope, name):
            await self._cmd_options_get(scope=scope, name=name)

        @options_group.command(name="set", help="Set an option value")
        @click.option("-g", "--global", "scope", flag_value="global",
                      help="Target global options")
        @click.option("-s", "--session", "scope", flag_value="session",
                      default=True, help="Target session options (default)")
        @click.argument("name")
        @click.argument("value")
        async def options_set(scope, name, value):
            await self._cmd_options_set(scope=scope, name=name, value=value)

        @registry.command(name="rename", help="Rename the current tab")
        @click.argument("name", nargs=-1, required=True)
        async def rename(name: tuple[str, ...]):
            await self._cmd_rename(" ".join(name))

        @registry.command(name="help", help="Show available commands and usage")
        @click.argument("command_name", default="", required=False)
        async def help_cmd(command_name: str):
            await self._cmd_help(command_name)

        @registry.command(name="quit", help="Quit the application")
        async def quit_cmd():
            self.app.exit()

        @registry.command(name="clear", help="Clear chat messages")
        async def clear():
            await self._cmd_clear()

        @registry.command(name="explore", help="Browse topics, entries, and flashcards")
        async def explore():
            await self._cmd_explore()

        @registry.group(name="resources", help="Resource management (ctrl+r to toggle panel)",
                        invoke_without_command=True)
        @click.pass_context
        async def resources_group(ctx):
            if ctx.invoked_subcommand is None:
                await self._cmd_resources()

        @resources_group.command(name="add", help="Add a resource from a file or webpage")
        @click.argument("source")
        @click.argument("name", required=False, default=None)
        @click.option("--topics", default=None, help="Comma-separated topic IDs or names to link")
        @click.option("--no-active-topic", is_flag=True, help="Don't auto-link to active topic")
        async def resources_add(source, name, topics, no_active_topic):
            await self._cmd_resources_add(source, name=name, topics=topics, no_active_topic=no_active_topic)

        @resources_group.command(name="new", help="Browse files and create a new resource")
        async def resources_new():
            await self._cmd_resources_new()

        @resources_group.command(name="link", help="Link a resource to topics (defaults to active topic)")
        @click.argument("resource_id")
        @click.argument("topic_ids", nargs=-1)
        async def resources_link(resource_id, topic_ids):
            await self._cmd_resources_link(resource_id, list(topic_ids))

        @resources_group.command(name="unlink", help="Unlink a resource from topics (defaults to active topic)")
        @click.argument("resource_id")
        @click.argument("topic_ids", nargs=-1)
        async def resources_unlink(resource_id, topic_ids):
            await self._cmd_resources_unlink(resource_id, list(topic_ids))

        @resources_group.command(name="delete", help="Delete a resource")
        @click.argument("resource_id")
        async def resources_delete(resource_id):
            await self._cmd_resources_delete(resource_id)

        @resources_group.command(name="extract-subsections", help="Extract subsections from a resource (PDF)")
        @click.argument("resource_id")
        async def resources_extract(resource_id):
            await self._cmd_resources_extract_subsections(resource_id)

        @registry.command(name="idle", help="Return to idle mode")
        async def idle():
            await self._cmd_idle()

        @registry.command(name="learn", help="Enter learning mode: set topic context")
        async def learn():
            await self._cmd_learn()

        @registry.command(name="review", help="Enter review mode: quizzes and practice")
        async def review():
            await self._cmd_review()

        @registry.command(name="new", help="Open a new chat session tab")
        async def new():
            await self._cmd_new()

        @registry.command(name="commit", help="Select learn-mode messages to commit as knowledge")
        @click.option("--auto", is_flag=True, help="Skip selection; let the agent draft a proposal from the conversation.")
        @click.argument("instructions", nargs=-1)
        async def commit(auto, instructions):
            await self._cmd_commit(auto=auto, instructions=" ".join(instructions) if instructions else "")

        @registry.command(name="logs", help="Open the logs viewer tab")
        async def logs():
            await self._cmd_logs()

        @registry.command(name="close", help="Close the current chat session tab")
        async def close():
            await self._cmd_close()

        if getattr(self.app, "debug_logging", False):
            @registry.command(name="test-flashcards", help="Open flashcard review widget with sample data")
            async def test_flashcards():
                await self._cmd_test_flashcards()

            @registry.command(name="test-flashcard-proposal", help="Open flashcard proposal widget with sample data")
            async def test_flashcard_proposal():
                await self._cmd_test_flashcard_proposal()

            @registry.command(name="test-commit-proposal", help="Open commit proposal widget with sample data")
            async def test_commit_proposal():
                await self._cmd_test_commit_proposal()

    async def _set_mode(
        self,
        mode: Mode,
        *,
        silent: bool = False,
        source: Literal["user", "agent"] = "user",
    ) -> None:
        """Set the session mode.

        Updates the TUI-level ``session_mode`` and (for user-initiated
        changes) queues a pending mode change on the agent middleware so
        graph state is updated on the next model call.

        Args:
            mode: The target mode.
            silent: Suppress the chat system message (e.g. for shift+tab
                cycling or agent-initiated changes).
            source: ``"user"`` for UI-initiated changes (shift+tab, slash
                commands) — these queue a pending change on the middleware.
                ``"agent"`` for tool-initiated changes — graph state is
                updated directly via ``Command``, so no pending change is
                needed.
        """

        # Branch variables:
        #   - silent
        #   - source
        #   - agent_busy

        # source is "agent"
        #   - Under this condition, these assertions must pass
        #       - agent_busy is True
        #       - silent is True
        if source == "agent":
            assert self._agent_busy
            silent = True # Just override
        
        # In all cases, if the new mode is no different from the current, just return early.
        if self.session_mode == mode:
            # Since "silent" is always true when source is "agent", this should never fire while the agent is busy.
            if not silent:
                self.append_message(
                    ChatMessageData(role=Role.SYSTEM, content=f"Already in {mode.value} mode.")
                )
            return
        
        # In this case:
        #   - Mutate self.session_mode and propagate to UI
        #   - Graph state is updated by the agent tool call
        #   - Clear pending user-initated mode changes - agent tool calls take priority.
        if source == "agent":
            self.session_mode = mode
            self.update_status_bar()

            # Clearing the pending user-initiated mode change addresses this edge case:
            #   - User sets mode to A while agent is running, then agent runs set_tool to change mode to B
            #
            # The agent's tool call wins in this case, and we discard the user-initiated change.
            await self._agent_session._mode_middleware.clear_pending_user_mode()
            return

        # From hereron-in, source is "user"
        message = "Returned to idle mode." if mode == Mode.IDLE else f"Entered {mode.value} mode."
        if self._agent_busy:
            # Mutate self.session_mode
            self.session_mode = mode
            
            # Post a pending user mode change to the agent. This gets intercepted before the next model call and is used
            # to automatically update the mode in the agnet state graph.
            if self._agent_session is not None:
                await self._agent_session.set_pending_user_mode(mode.value)

            if not silent:
                # append message to _UI only_.
                # set_pending_user_mode submits a message directly to the agent _when_ the queue is drained, which is
                # directly before the next model invocation (within)
                self.append_message(
                    ChatMessageData(role=Role.SYSTEM, content=message),
                    ui_only=True
                )
        
        else:
            self.session_mode = mode
            if silent:
                # Just post a notification to the agent
                if self._agent_session is not None:
                    self._agent_session.add_system_notification(message)
            else:
                # Post a notification to both the UI, and the agent.
                self.append_message(ChatMessageData(role=Role.SYSTEM, content=message))

        # Propagate to UI
        self.update_status_bar()

    async def _cmd_idle(self) -> None:
        await self._set_mode(Mode.IDLE)

    async def _cmd_learn(self) -> None:
        await self._set_mode(Mode.LEARN)

    async def _cmd_review(self) -> None:
        await self._set_mode(Mode.REVIEW)

    async def _cmd_clear(self) -> None:
        """Clear all visible chat messages from the message area."""
        area = self.query_one("#message-area")
        await area.remove_children()
        self.messages.clear()
        self._active_widgets.clear()

    async def _cmd_explore(self) -> None:
        """Browse topics, entries, and flashcards."""
        existing = list(self.query(ExplorerViewer))
        if existing:
            tree = existing[-1]
            tree.focus()
        else:
            area = self.query_one("#message-area")
            tree = ExplorerViewer(session_factory=self._session_factory, id="explorer")
            await area.mount(tree)
            area.scroll_end(animate=False)
            self._active_widgets.append(tree)
            tree.focus()
        self.query_one("#chat-input").placeholder = (
            "Ctrl+l to refocus chat input"
        )

    def _create_resource_viewer(self) -> ResourceViewer:
        """Create a new ResourceViewer instance with the shared view model."""
        return ResourceViewer(
            session_factory=self._session_factory,
            resource_manager=self._resource_manager,
            view_model=self._resource_viewer_vm,
            id="resource-viewer",
        )

    async def _cmd_resources(self) -> None:
        """Toggle the resources panel."""
        dock = self.get_dock_area(self._resource_viewer_dock_id)
        if self._resource_viewer is not None and dock.visible:
            dock.hide()
            self.query_one("#chat-input", ChatInput).focus()
        else:
            if self._resource_viewer is None:
                self._resource_viewer = self._create_resource_viewer()
                await self.mount_to_dock_area(self._resource_viewer, self._resource_viewer_dock_id)
            else:
                dock.show()
            self._resource_viewer.focus()

    async def _cmd_resources_new(self) -> None:
        """Open a multi-step modal to create a new resource."""
        from rhizome.tui.screens.new_resource import NewResourceScreen
        self.app.push_screen(
            NewResourceScreen(session_factory=self._session_factory),
            callback=self._on_new_resource_confirmed,
        )

    def _on_new_resource_confirmed(self, result) -> None:
        if result is None:
            return
        self.run_worker(self._ingest_new_resource(result))

    async def _ingest_new_resource(self, result) -> None:
        """Ingest a resource from the NewResourceScreen result."""
        from rhizome.tui.screens.new_resource import NewResourceResult
        result: NewResourceResult

        try:
            raw_text = extract_text_from_file(str(result.path))
        except Exception as e:
            self.append_message(ChatMessageData(role=Role.SYSTEM, content=f"Error reading file: {e}"))
            return

        if not raw_text.strip():
            self.append_message(ChatMessageData(role=Role.SYSTEM, content="No text content extracted from file."))
            return

        # Mount a container that holds the spinner now and the result message later,
        # so it stays in position even if the user sends messages while we work.
        area = self.query_one("#message-area", VerticalScroll)
        container = Vertical(classes="--ingest-container")
        container.styles.height = "auto"
        await area.mount(container)
        spinner = Spinner("generating metadata...")
        await container.mount(spinner)
        area.scroll_end(animate=False)

        # Auto-generate metadata (title + summary) via LLM.
        name = result.name
        summary: str | None = None
        metadata_tokens: int | None = None
        try:
            llm = init_chat_model("claude-haiku-4-5-20251001", api_key=get_api_key(), temperature=0.0)
            meta_result = await generate_resource_metadata(llm, raw_text)
            summary = meta_result.metadata.summary
            metadata_tokens = meta_result.total_tokens
            if name is None:
                name = meta_result.metadata.title
        except Exception as e:
            self._log.warning("Auto-metadata generation failed: %s", e)
            if name is None:
                name = result.path.stem
        finally:
            await spinner.remove()

        # Use topics from the modal, falling back to active topic.
        topic_ids: list[int] = list(result.topic_ids)
        if not topic_ids and self.active_topic is not None:
            topic_ids.append(self.active_topic.id)

        # Read source bytes for formats that support subsection extraction.
        source_type = result.path.suffix.lstrip(".").lower() or None
        try:
            source_bytes = result.path.read_bytes()
        except Exception:
            source_bytes = None

        try:
            resource_id, estimated_tokens = await ingest_resource(
                self._session_factory,
                name=name,
                raw_text=raw_text,
                topic_ids=topic_ids or None,
                loading_preference=result.loading_preference,
                summary=summary,
                source_type=source_type,
                source_bytes=source_bytes,
            )
        except Exception as e:
            self.append_message(ChatMessageData(role=Role.SYSTEM, content=f"Error creating resource: {e}"))
            return

        parts = [f"Resource [{resource_id}] '{name}' created (~{_fmt_tokens(estimated_tokens)} tokens, pref={result.loading_preference.value})"]
        if topic_ids:
            parts.append(f"Linked to topic(s): {', '.join(str(t) for t in topic_ids)}")
        if metadata_tokens is not None:
            parts.append(f"(~{_fmt_tokens(metadata_tokens)} tokens used in generating summary)")
        msg = RichChatMessage(role=Role.SYSTEM, content=".  ".join(parts) + ".")
        await container.mount(msg)

    async def _cmd_resources_add(
        self,
        source: str,
        *,
        name: str | None = None,
        topics: str | None = None,
        no_active_topic: bool = False,
    ) -> None:
        """Add a resource from a file path or webpage URL."""
        is_url = source.startswith("http://") or source.startswith("https://")

        # Extract text
        try:
            if is_url:
                self.append_message(ChatMessageData(role=Role.SYSTEM, content=f"Fetching {source}..."))
                raw_text = await fetch_webpage_text(source)
            else:
                raw_text = extract_text_from_file(source)
        except Exception as e:
            self.append_message(ChatMessageData(role=Role.SYSTEM, content=f"Error reading source: {e}"))
            return

        if not raw_text.strip():
            self.append_message(ChatMessageData(role=Role.SYSTEM, content="No text content extracted from source."))
            return

        # Derive name from source if not provided
        if not name:
            if is_url:
                from urllib.parse import urlparse
                name = urlparse(source).path.rstrip("/").rsplit("/", 1)[-1] or urlparse(source).netloc
            else:
                name = Path(source).stem

        # Build topic ID list
        topic_ids: list[int] = []
        if topics:
            identifiers = [t.strip() for t in topics.split(",") if t.strip()]
            resolved = await self._resolve_identifiers(topics=identifiers)
            if resolved is None:
                return
            topic_ids = resolved["topics"]

        if not no_active_topic and self.active_topic is not None:
            if self.active_topic.id not in topic_ids:
                topic_ids.append(self.active_topic.id)

        # Ingest
        try:
            resource_id, estimated_tokens = await ingest_resource(
                self._session_factory,
                name=name,
                raw_text=raw_text,
                topic_ids=topic_ids or None,
            )
        except Exception as e:
            self.append_message(ChatMessageData(role=Role.SYSTEM, content=f"Error creating resource: {e}"))
            return


        parts = [f"Resource [{resource_id}] '{name}' created (~{_fmt_tokens(estimated_tokens)} tokens)"]
        if topic_ids:
            parts.append(f"Linked to topic(s): {', '.join(str(t) for t in topic_ids)}")
        self.append_message(ChatMessageData(role=Role.SYSTEM, content=".  ".join(parts) + "."))

    # ------------------------------------------------------------------
    # Name resolution helpers
    # ------------------------------------------------------------------

    async def _resolve_identifiers(
        self,
        *,
        resource: str | None = None,
        topics: list[str] | None = None,
    ) -> dict[str, int | list[int]] | None:
        """Resolve resource and/or topic identifiers, batching all ambiguities.

        Returns a dict with ``"resource"`` (int) and/or ``"topics"`` (list[int])
        keys on success, or ``None`` on error/cancel.  Only keys whose arguments
        were provided are included in the result.
        """
        specs: list[_AmbiguousSpec] = []
        result: dict[str, int | list[int]] = {}

        # Placeholder bookkeeping for ambiguous topics: index in topic_ids → spec key.
        topic_ids: list[int] = []
        ambiguous_topic_map: dict[str, int] = {}  # spec key → index in topic_ids

        async with self._session_factory() as session:
            if resource is not None:
                try:
                    res = await resolve_resource(session, resource)
                except ValueError as e:
                    self.append_message(ChatMessageData(role=Role.SYSTEM, content=str(e)))
                    return None
                if isinstance(res, list):
                    specs.append(_AmbiguousSpec(
                        key="resource",
                        prompt=f"Multiple resources match '{resource}':",
                        options=[f"[{ar.resource.id}] {ar.resource.name}" for ar in res],
                    ))
                else:
                    result["resource"] = res.id

            if topics is not None:
                for i, ident in enumerate(topics):
                    try:
                        top = await resolve_topic(session, ident)
                    except ValueError as e:
                        self.append_message(ChatMessageData(role=Role.SYSTEM, content=str(e)))
                        return None
                    if isinstance(top, list):
                        key = f"topic:{i}"
                        specs.append(_AmbiguousSpec(
                            key=key,
                            prompt=f"Multiple topics match '{ident}':",
                            options=[f"[{at.topic.id}] {at.path}" for at in top],
                        ))
                        ambiguous_topic_map[key] = len(topic_ids)
                        topic_ids.append(-1)  # placeholder
                    else:
                        topic_ids.append(top.id)

        # Disambiguate all at once
        if specs:
            answers = await self._disambiguate_identifiers(specs)
            if answers is None:
                return None
            if "resource" in answers:
                result["resource"] = answers["resource"]
            for key, idx in ambiguous_topic_map.items():
                topic_ids[idx] = answers[key]

        if topics is not None:
            result["topics"] = topic_ids

        return result

    async def _disambiguate_identifiers(self, specs: list[_AmbiguousSpec]) -> dict[str, int] | None:
        """Show a disambiguation widget for all ambiguous specifiers at once.

        Uses ``Choices`` for a single spec, ``MultipleChoices`` for several.
        A "Cancel" option is appended to each question.

        Returns ``{spec.key: resolved_id}`` on success, or ``None`` on cancel.
        """
        if not specs:
            return {}

        questions = [
            {"name": s.key, "prompt": s.prompt, "options": s.options}
            for s in specs
        ]

        area = self.query_one("#message-area", VerticalScroll)
        if len(questions) == 1:
            q = questions[0]
            widget = Choices(prompt=q["prompt"], options=q["options"])
        else:
            widget = MultipleChoices(questions=questions)

        await area.mount(widget)
        area.scroll_end(animate=False)

        chat_input = self.query_one("#chat-input", ChatInput)
        chat_input.disabled = True
        chat_input.placeholder = "Select an option above..."
        self._active_widgets.append(widget)
        widget.focus()

        try:
            raw = await widget.wait_for_selection()
        except asyncio.CancelledError:
            return None
        finally:
            self._restore_chat_input()

        # Normalise: Choices returns a bare string; MultipleChoices returns a dict.
        if isinstance(raw, str):
            results = {questions[0]["name"]: raw}
        else:
            results = raw

        # Extract numeric IDs from "[123] label" format.
        return {k: int(v.split("]")[0].lstrip("[")) for k, v in results.items()}

    async def _cmd_resources_link(self, resource_identifier: str, topic_identifiers: list[str]) -> None:
        """Link a resource to one or more topics."""
        resolved = await self._resolve_identifiers(
            resource=resource_identifier,
            topics=topic_identifiers or None,
        )
        if resolved is None:
            return
        resource_id = resolved["resource"]

        if "topics" in resolved:
            topic_ids = resolved["topics"]
        elif self.active_topic is not None:
            topic_ids = [self.active_topic.id]
        else:
            self.append_message(ChatMessageData(role=Role.SYSTEM, content="No topic IDs provided and no active topic set."))
            return

        try:
            async with self._session_factory() as session:
                for tid in topic_ids:
                    await link_resource_to_topic(session, resource_id=resource_id, topic_id=tid)
                await session.commit()
        except Exception as e:
            self.append_message(ChatMessageData(role=Role.SYSTEM, content=f"Error linking resource: {e}"))
            return

        self.append_message(ChatMessageData(
            role=Role.SYSTEM,
            content=f"Resource [{resource_id}] linked to topic(s): {', '.join(str(t) for t in topic_ids)}.",
        ))

    async def _cmd_resources_unlink(self, resource_identifier: str, topic_identifiers: list[str]) -> None:
        """Unlink a resource from one or more topics."""
        resolved = await self._resolve_identifiers(
            resource=resource_identifier,
            topics=topic_identifiers or None,
        )
        if resolved is None:
            return
        resource_id = resolved["resource"]

        if "topics" in resolved:
            topic_ids = resolved["topics"]
        elif self.active_topic is not None:
            topic_ids = [self.active_topic.id]
        else:
            self.append_message(ChatMessageData(role=Role.SYSTEM, content="No topic IDs provided and no active topic set."))
            return

        try:
            async with self._session_factory() as session:
                for tid in topic_ids:
                    await unlink_resource_from_topic(session, resource_id=resource_id, topic_id=tid)
                await session.commit()
        except Exception as e:
            self.append_message(ChatMessageData(role=Role.SYSTEM, content=f"Error unlinking resource: {e}"))
            return

        self.append_message(ChatMessageData(
            role=Role.SYSTEM,
            content=f"Resource [{resource_id}] unlinked from topic(s): {', '.join(str(t) for t in topic_ids)}.",
        ))

    async def _cmd_resources_delete(self, resource_identifier: str) -> None:
        """Delete a resource."""
        resolved = await self._resolve_identifiers(resource=resource_identifier)
        if resolved is None:
            return
        resource_id = resolved["resource"]
        try:
            async with self._session_factory() as session:
                await delete_resource(session, resource_id)
                await session.commit()
        except Exception as e:
            self.append_message(ChatMessageData(role=Role.SYSTEM, content=f"Error deleting resource: {e}"))
            return

        self.append_message(ChatMessageData(
            role=Role.SYSTEM,
            content=f"Resource [{resource_id}] deleted.",
        ))

    async def _cmd_resources_extract_subsections(self, resource_identifier: str) -> None:
        """Extract subsections from a resource document."""
        from rhizome.resources.extraction import (
            extract_document_subsections,
            estimate_extraction_tokens,
            Section,
        )

        # Resolve the resource.
        resolved = await self._resolve_identifiers(resource=resource_identifier)
        if resolved is None:
            return
        resource_id = resolved["resource"]

        # Load the resource and validate it has source bytes.
        async with self._session_factory() as session:
            resource = await get_resource(session, resource_id)
        if resource is None:
            self.append_message(ChatMessageData(role=Role.SYSTEM, content=f"Resource {resource_id} not found."))
            return
        if not resource.content or not resource.content.source_bytes or not resource.source_type:
            self.append_message(ChatMessageData(
                role=Role.SYSTEM,
                content=f"Resource [{resource_id}] has no source file stored. "
                        "Subsection extraction requires the original document bytes.",
            ))
            return

        # Estimate token cost and prompt for confirmation.
        doc_tokens = resource.estimated_tokens or 0
        estimated_cost = estimate_extraction_tokens(doc_tokens)
        prompt = (
            f"Extract subsections from '{resource.name}'?\n"
            f"Document: ~{_fmt_tokens(doc_tokens)} tokens.  "
            f"Estimated cost: ~{_fmt_tokens(estimated_cost)} tokens."
        )

        area = self.query_one("#message-area", VerticalScroll)
        confirm_widget = Choices(prompt=prompt, options=["Proceed", "Cancel"])
        await area.mount(confirm_widget)
        area.scroll_end(animate=False)
        chat_input = self.query_one("#chat-input", ChatInput)
        chat_input.disabled = True
        chat_input.placeholder = "Select an option above..."
        self._active_widgets.append(confirm_widget)
        confirm_widget.focus()
        try:
            selected = await confirm_widget.wait_for_selection()
        except asyncio.CancelledError:
            return
        finally:
            self._restore_chat_input()

        if selected != "Proceed":
            return

        # Run extraction with a spinner.
        container = Vertical(classes="--extract-container")
        container.styles.height = "auto"
        await area.mount(container)
        spinner = Spinner("extracting subsections...")
        await container.mount(spinner)
        area.scroll_end(animate=False)

        try:
            llm = init_chat_model("claude-sonnet-4-6", api_key=get_api_key(), temperature=0.0)
            sections, _extraction, stats = await extract_document_subsections(
                resource.content.source_bytes,
                resource.source_type,
                llm,
            )
        except Exception as e:
            await spinner.remove()
            msg = RichChatMessage(role=Role.SYSTEM, content=f"Extraction failed: {e}")
            await container.mount(msg)
            return
        finally:
            if spinner.parent is not None:
                await spinner.remove()

        # Write sections to the database.
        try:
            async with self._session_factory() as session:
                await insert_sections(session, resource_id, sections)
                await link_chunks_to_sections(session, resource_id)
                await session.commit()
        except Exception as e:
            msg = RichChatMessage(role=Role.SYSTEM, content=f"Error saving sections: {e}")
            await container.mount(msg)
            return

        # Final summary message.
        parts = [f"Extracted {stats.sections_accepted} subsections from '{resource.name}'"]
        if stats.total_tokens is not None:
            parts.append(f"(~{_fmt_tokens(stats.total_tokens)} tokens used)")
        msg = RichChatMessage(role=Role.SYSTEM, content=".  ".join(parts) + ".")
        await container.mount(msg)

    def action_refocus_resources(self) -> None:
        """Ctrl+R — focus the resources panel if visible."""
        if self._resource_viewer is not None and self.get_dock_area(self._resource_viewer_dock_id).visible:
            self._resource_viewer.focus()

    async def action_cycle_dock_position(self) -> None:
        """Ctrl+D — cycle the resource viewer between dock positions."""
        if self._resource_viewer is None:
            return
        cycle = {
            "dock-bottom": "dock-left",
            "dock-left": "dock-right",
            "dock-right": "dock-bottom",
        }
        new_dock_id = cycle[self._resource_viewer_dock_id]
        await self.mount_to_dock_area(self._resource_viewer, new_dock_id)
        self._resource_viewer_dock_id = new_dock_id
        self._resource_viewer.focus()

    async def _cmd_test_flashcards(self) -> None:
        """Open the flashcard review widget with sample data.

        Uses fake IDs, so we stub out both the session factory and the
        module-level ``apply_rating`` import in the VM module — otherwise
        the DB call inside ``Flashcard.set_score`` / ``Flashcard.again``
        errors on the unknown row.
        """
        from datetime import datetime, timedelta
        from types import SimpleNamespace

        from rhizome.tui.widgets.flashcard_review import view_model as _vm_module

        sample_cards = [
            {"id": 101, "question": "What is the time complexity of binary search?", "answer": "O(log n) — each comparison halves the remaining search space."},
            {"id": 102, "question": "Explain the difference between a stack and a queue.", "answer": "A stack is LIFO (Last In, First Out): the most recently added element is removed first.\n\nA queue is FIFO (First In, First Out): the earliest added element is removed first."},
            {"id": 103, "question": "What is a hash collision and how is it typically resolved?", "answer": "A hash collision occurs when two different keys produce the same hash value.\n\nCommon resolution strategies:\n• Chaining — each bucket holds a linked list of entries\n• Open addressing — probe for the next available slot (linear, quadratic, or double hashing)"},
            {"id": 204, "question": "What does the CAP theorem state?", "answer": "A distributed system can provide at most two of the following three guarantees simultaneously:\n\n• Consistency — every read returns the most recent write\n• Availability — every request receives a response\n• Partition tolerance — the system operates despite network partitions"},
            {"id": 205, "question": "What is the difference between concurrency and parallelism?", "answer": "Concurrency is about dealing with multiple tasks at once (structure).\nParallelism is about doing multiple tasks at once (execution).\n\nConcurrency is possible on a single core via interleaving; parallelism requires multiple cores."},
        ]

        # --- Stubbed session factory + apply_rating ---------------------
        class _FakeSession:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): return False

        def _fake_session_factory(): return _FakeSession()

        async def _fake_apply_rating(session, card_id, rating):
            # Short due delay so AGAIN'd cards re-appear quickly for
            # manual testing of the AWAITING_REVEAL flow.
            return SimpleNamespace(due=datetime.now() + timedelta(seconds=8))

        _vm_module.apply_rating = _fake_apply_rating

        # --- Fake scorer ------------------------------------------------
        # Tweak this mapping to test different auto-score scenarios.
        # Anything omitted triggers the failure-fallback (card bounces
        # back to REVEALED_NOT_SCORED with auto_scoring_failed=True).
        auto_score_results = {
            101: 3,  # good
            102: 1,  # again — requeued
            103: 2,  # hard
            204: 4,  # easy
            # 205 intentionally omitted — exercises the failure path.
        }

        class _FakeScorer:
            def __init__(self, results_by_id: dict[int, int]):
                self._results_by_id = results_by_id
                self.structured_response = None

            async def ainvoke(self, prompt: str):
                # Simulate scorer latency so the throbber is visible.
                await asyncio.sleep(1.5)
                results = [
                    SimpleNamespace(flashcard_id=i, score=s, feedback="")
                    for i, s in self._results_by_id.items()
                ]
                self.structured_response = SimpleNamespace(results=results)

        area = self.query_one("#message-area")
        review = FlashcardReview(
            cards=sample_cards,
            session_factory=_fake_session_factory,
            auto_score_enabled=True,
            auto_scorer=_FakeScorer(auto_score_results),
        )
        await area.mount(review)
        area.scroll_end(animate=False)
        self._active_widgets.append(review)
        review.focus()
        self.query_one("#chat-input").placeholder = (
            "Ctrl+l to refocus chat input"
        )

    async def _cmd_test_flashcard_proposal(self) -> None:
        """Open the flashcard proposal widget with sample data."""
        sample_cards = [
            {
                "question": "What is the time complexity of binary search?",
                "answer": "O(log n) — each comparison halves the remaining search space.",
                "testing_notes": "Ask for best/worst case separately.",
                "entry_ids": [1, 3],
            },
            {
                "question": "Explain the difference between a stack and a queue.",
                "answer": "A stack is LIFO (Last In, First Out): the most recently added element is removed first.\n\nA queue is FIFO (First In, First Out): the earliest added element is removed first.",
                "testing_notes": None,
                "entry_ids": [2],
            },
            {
                "question": "What is a hash collision and how is it typically resolved?",
                "answer": "A hash collision occurs when two different keys produce the same hash value.\n\nCommon resolution strategies:\n• Chaining — each bucket holds a linked list of entries\n• Open addressing — probe for the next available slot (linear, quadratic, or double hashing)",
                "testing_notes": "Can ask about chaining vs open addressing trade-offs as a follow-up.",
                "entry_ids": [],
            },
        ]

        area = self.query_one("#message-area")
        proposal = FlashcardProposal(flashcards=sample_cards)
        await area.mount(proposal)
        area.scroll_end(animate=False)
        self._active_widgets.append(proposal)
        proposal.focus()
        self.query_one("#chat-input").placeholder = (
            "Ctrl+l to refocus chat input"
        )

    async def _cmd_test_commit_proposal(self) -> None:
        """Open the commit proposal widget with sample data."""
        sample_entries = [
            {
                "title": "Binary Search",
                "content": "Binary search is a divide-and-conquer algorithm that finds a target value in a sorted array by repeatedly halving the search space.\n\nTime complexity: O(log n)\nSpace complexity: O(1) iterative, O(log n) recursive",
                "entry_type": "fact",
                "topic_id": 1,
            },
            {
                "title": "Hash Table Collision Resolution",
                "content": "When two keys hash to the same bucket, a collision occurs. Common strategies:\n\n• Chaining — each bucket holds a linked list\n• Open addressing — probe for the next available slot\n• Robin Hood hashing — minimize probe distance variance",
                "entry_type": "exposition",
                "topic_id": 1,
            },
            {
                "title": "CAP Theorem Overview",
                "content": "The CAP theorem states that a distributed system can provide at most two of three guarantees: Consistency, Availability, and Partition tolerance.\n\nIn practice, partition tolerance is non-negotiable, so the real trade-off is between consistency and availability.",
                "entry_type": "overview",
                "topic_id": 2,
            },
        ]
        sample_topic_map = {
            1: "Data Structures",
            2: "Distributed Systems",
        }

        area = self.query_one("#message-area")
        proposal = CommitProposal(entries=sample_entries, topic_map=sample_topic_map)
        await area.mount(proposal)
        area.scroll_end(animate=False)
        self._active_widgets.append(proposal)
        proposal.focus()
        self.query_one("#chat-input").placeholder = (
            "Ctrl+l to refocus chat input"
        )

    async def _cmd_options(self, *, edit: bool = False, scope: str = "session") -> None:
        """Open settings and configuration."""
        is_global = scope == "global"
        target = self.app.options if is_global else self.options  # type: ignore[attr-defined]

        if edit:
            jsonc_text = build_jsonc_snapshot(target)
            editor = os.environ.get("EDITOR", "nano")

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".jsonc", prefix="rhizome-options-", delete=False
            ) as tmp:
                tmp.write(jsonc_text)
                tmp_path = tmp.name

            try:
                with self.app.suspend():
                    subprocess.run([editor, tmp_path])

                new_text = Path(tmp_path).read_text(encoding="utf-8")
                new_opts = parse_jsonc(new_text)

                spec_map = {s.resolved_name: s for s in Options.spec()}
                changed: list[str] = []
                for key, val in new_opts.items():
                    s = spec_map.get(key)
                    if s is not None and target.get(s) != val:
                        await target.set(s, val)
                        changed.append(key)

                await target.post_update()
                if changed:
                    names = ", ".join(f"`{n}`" for n in changed)
                    msg = f"Options updated: {names}"
                else:
                    msg = "No changes made."
                self.append_message(
                    ChatMessageData(role=Role.SYSTEM, content=msg)
                )
            except Exception as exc:
                self.append_message(
                    ChatMessageData(role=Role.SYSTEM, content=f"Error applying options: {exc}")
                )
            finally:
                os.unlink(tmp_path)
            return

        # Inline widget mode — dismiss any existing editor first
        for ed in self.query(OptionsEditor):
            await ed.remove()

        area = self.query_one("#message-area")
        editor_widget = OptionsEditor(target, id="options-editor")
        await area.mount(editor_widget)
        area.scroll_end(animate=False)
        editor_widget.focus()

    async def _cmd_options_get(self, *, scope: str = "session", name: str) -> None:
        """Print the current value of a single option."""
        spec_map = {s.resolved_name: s for s in Options.spec()}
        spec = spec_map.get(name)
        if spec is None:
            self.append_message(
                ChatMessageData(role=Role.SYSTEM, content=f"Unknown option: {name}")
            )
            return
        is_global = scope == "global"
        target = self.app.options if is_global else self.options  # type: ignore[attr-defined]
        value = target.get(spec)
        self.append_message(
            ChatMessageData(role=Role.SYSTEM, content=f"{name} = {value!r}")
        )

    async def _cmd_options_set(self, *, scope: str = "session", name: str, value: str) -> None:
        """Set an option value."""
        spec_map = {s.resolved_name: s for s in Options.spec()}
        spec = spec_map.get(name)
        if spec is None:
            self.append_message(
                ChatMessageData(role=Role.SYSTEM, content=f"Unknown option: {name}")
            )
            return
        is_global = scope == "global"
        target = self.app.options if is_global else self.options  # type: ignore[attr-defined]
        try:
            coerced = spec.from_string(value)
            await target.set(spec, coerced)
            await target.post_update()
            self.append_message(
                ChatMessageData(role=Role.SYSTEM, content=f"{name} set to {coerced!r}")
            )
        except (ValueError, TypeError) as exc:
            self.append_message(
                ChatMessageData(role=Role.SYSTEM, content=f"Error setting {name}: {exc}")
            )

    async def _cmd_help(self, command_name: str = "") -> None:
        """Show available commands, or details for a specific command."""
        if command_name:
            name = command_name.strip().lstrip("/")
            cmd = self._command_registry.commands.get(name)
            if cmd is None:
                text = f"Unknown command: /{name}\nType /help to see available commands."
            else:
                # Get help text from click command
                with cmd.make_context(name, [], max_content_width=self._command_registry.max_content_width) as ctx:
                    text = ctx.get_help()
        else:
            lines = ["**Available commands:**", ""]
            for name in sorted(self._command_registry.commands):
                cmd = self._command_registry.commands[name]
                # Use the callback's docstring or the click help string
                desc = cmd.help or (cmd.callback.__doc__ if cmd.callback else "") or ""
                # Take only the first line of the description
                desc = desc.strip().split("\n")[0] if desc else ""
                lines.append(f"  /{name} — {desc}")
            lines.append("")
            lines.append("Commands support standard CLI syntax (options, flags, --help).")
            lines.append("e.g. /options --edit --global, /options set agent.temperature 0.5")
            text = "\n".join(lines)

        self.append_message(ChatMessageData(role=Role.SYSTEM, content=text, rich=True))

    async def _cmd_rename(self, name: str) -> None:
        """Rename the active chat session tab."""
        from rhizome.tui.screens.main import ChatTabPane

        new_name = name.strip()
        if not new_name:
            self.append_message(
                ChatMessageData(role=Role.SYSTEM, content="Usage: /rename <name>")
            )
            return

        tabs = self.app.screen.query_one("#tabs", TabbedContent)
        active_pane = tabs.active_pane
        if active_pane is not None and isinstance(active_pane, ChatTabPane):
            active_pane.full_name = new_name
            active_pane._update_tab_label()

    async def _cmd_new(self) -> None:
        """Create a new chat session tab."""
        from rhizome.tui.screens.main import MainScreen

        screen = self.app.screen
        if isinstance(screen, MainScreen):
            await screen._add_tab()

    async def _cmd_commit(self, *, auto: bool = False, instructions: str = "") -> None:
        """Select learn-mode messages to commit as knowledge."""
        if auto:
            notification = (
                "User requested an automatic commit. Review the conversation history and your "
                "knowledge of the user's database, then use commit_proposal_create to draft "
                "knowledge entries based on your own judgment. Present the proposal to the user."
            )
            if instructions:
                notification += f"\n\nUser provided these additional instructions:\n{instructions}"
            self._agent_session.add_system_notification(notification)
            self._start_agent()
            return
        self.enter_commit_mode()

    async def _cmd_logs(self) -> None:
        """Open the logs tab."""
        from rhizome.tui.screens.main import MainScreen

        screen = self.app.screen
        if isinstance(screen, MainScreen):
            await screen._add_log_tab()

    async def _cmd_close(self) -> None:
        """Close the current chat session tab."""
        from rhizome.tui.screens.main import MainScreen

        screen = self.app.screen
        if isinstance(screen, MainScreen):
            await screen._close_active_tab()

    # ------------------------------------------------------------------
    # Child widget events (topic tree, options editor)
    # ------------------------------------------------------------------

    def on_active_topic_changed(self, event: ActiveTopicChanged) -> None:
        self.active_topic = event.topic
        self._topic_path = event.path
        self.update_status_bar()
        if self._resource_viewer is not None:
            self._resource_viewer.set_active_topic(event.topic, event.path)
        if event.topic is not None:
            self.append_message(ChatMessageData(role=Role.SYSTEM, content=f"Selected topic: {event.topic.name}"))
        else:
            self.append_message(ChatMessageData(role=Role.SYSTEM, content="Cleared active topic"))
        # If posted from the explorer viewer, dismiss it.
        for viewer in self.query(ExplorerViewer):
            viewer.remove()
            self._restore_chat_input()

    def on_explorer_viewer_dismissed(self, event: ExplorerViewer.Dismissed) -> None:
        for viewer in self.query(ExplorerViewer):
            viewer.remove()
        self._restore_chat_input()

    def notify_database_committed(self, event: DatabaseCommitted) -> None:
        """Route DB change notifications to data-displaying widgets."""
        for viewer in self.query(ExplorerViewer):
            viewer.run_worker(viewer.notify_database_committed(event))
        if self._resource_viewer is not None and self.get_dock_area(self._resource_viewer_dock_id).visible:
            self._resource_viewer.run_worker(self._resource_viewer.notify_database_committed(event))

    def on_resource_viewer_dismissed(self, event: ResourceViewer.Dismissed) -> None:
        self.get_dock_area(self._resource_viewer_dock_id).hide()
        self._restore_chat_input()

    def on_options_editor_dismissed(self, event: OptionsEditor.Dismissed) -> None:
        for ed in self.query(OptionsEditor):
            ed.remove()
        self._restore_chat_input()

    def on_options_editor_done(self, event: OptionsEditor.Done) -> None:
        editors = list(self.query(OptionsEditor))
        for ed in editors:
            ed.remove()
        self._restore_chat_input()

        if event.changes:
            lines = [f"  {name}: {old} → {new}" for name, (old, new) in event.changes.items()]
            self.append_message(ChatMessageData(
                role=Role.SYSTEM, content="Options changed:\n" + "\n".join(lines), rich=True
            ))

        async def _notify() -> None:
            if self.options is not None:
                await self.options.post_update()

        self.run_worker(_notify())

    def on_hint_higher_verbosity(self, event: HintHigherVerbosity) -> None:
        now = time.monotonic()
        elapsed = now - self._verbosity_hint_last_shown
        if self._verbosity_hint_allowed or elapsed >= 600:
            self.notify(
                "Hint: the agent has indicated that a higher verbosity "
                "may be required to properly answer your query.",
                timeout=8
            )
            self._verbosity_hint_allowed = False
            self._verbosity_hint_last_shown = now

    def on_commit_approved(self, event: CommitApproved) -> None:
        self.append_message(ChatMessageData(
            role=Role.SYSTEM,
            content=f"Committed {event.count} knowledge entry/entries to the database.",
        ))

    # ------------------------------------------------------------------
    # Commit mode
    # ------------------------------------------------------------------

    def enter_commit_mode(self) -> None:
        """Activate commit mode: decorate selectable messages for selection."""

        # Determine which messages are selectable based on the commit_selectable option.
        level = self.options.get(Options.CommitSelectable) if self.options else "learn_only"
        if level == "all_agent":
            selector = "ChatMessage.agent-message"
        elif level == "all":
            selector = "ChatMessage.user-message, ChatMessage.agent-message"
        else:
            selector = "ChatMessage.agent-message.learn-mode"

        selectable = list(self.query(selector))
        if not selectable:
            self.append_message(ChatMessageData(role=Role.SYSTEM, content="No selectable messages to commit."))
            return

        self._commit.active = True
        self._commit.selectable = selectable
        self._commit.cursor = 0
        self._commit.selected = set()

        # Decorate each message with a checkbox and the --commit-selectable class, mount a checkbox
        # to the message, and set the first message as the initial cursor position.
        for msg in self._commit.selectable:
            msg.add_class("--commit-selectable")
            checkbox = Static("☐", classes="commit-checkbox")
            msg.mount(checkbox, before=0)

        self._commit.selectable[0].add_class("--commit-cursor")
        self._commit.selectable[0].scroll_visible()

        # Disable the chat input and show commit mode instructions in the placeholder.
        chat_input = self.query_one("#chat-input", ChatInput)
        chat_input.disabled = True
        chat_input.placeholder = "↑↓ navigate  Space select  Ctrl+Enter confirm  Esc cancel"

    def confirm_commit_selection(self) -> None:
        """Deactivate commit mode, clean up decorations, and prompt for optional instructions."""

        # Clean up all commit-mode decorations and state.
        for msg in self._commit.selectable:
            msg.remove_class("--commit-selectable")
            msg.remove_class("--commit-cursor")
            msg.remove_class("--commit-selected")
            for cb in msg.query(".commit-checkbox"):
                cb.remove()

        # Remark: we do NOT clear the commit state until either a CommitApproved or a CommitCanceled
        # event is received. This is because the commit subagent access the commit state directly, and
        # moreover can modify the commit selection (e.g. to deselect messages at user request) before
        # posting the final approval event.
        #
        # We merely deactivate the commit mode here to exit the selection UI and prevent further changes.
        self._commit.active = False

        if not self._commit.selected:
            self._restore_chat_input()
            self.append_message(ChatMessageData(role=Role.SYSTEM, content="No messages selected for commit."))
            return

        # Enter instructions phase: swap to the commit instructions input.
        self._show_commit_instructions()

    def _submit_commit(self, instructions: str = "") -> None:
        """Build the commit payload, inject it into agent state, and start the agent."""

        # Build the commit payload and inject it into agent state for the
        # commit tools to read via ToolRuntime.
        level = self.options.get(Options.CommitSelectable) if self.options else "learn_only"
        all_messages = list(self.query_one("#message-area").query(ChatMessage))
        commit_payload = []
        for idx in sorted(self._commit.selected):
            msg = self._commit.selectable[idx]
            entry: dict = {"index": idx, "content": msg.content_text}

            # For agent-only selection modes, include the preceding user message
            # as context so the commit agent understands what prompted the response.
            if level != "all" and msg._role == Role.AGENT:
                msg_pos = all_messages.index(msg)
                for prev in reversed(all_messages[:msg_pos]):
                    if prev._role == Role.USER:
                        entry["user_context"] = prev.content_text
                        break
                    if prev._role == Role.AGENT:
                        break

            commit_payload.append(entry)
        self._agent_session.set_commit_payload(commit_payload)

        # Compute the approximate token count for the selected messages, and determine routing based on subagent commit options.
        combined = "\n".join(entry["content"] for entry in commit_payload)
        approx_tokens = count_tokens_approximately([HumanMessage(content=combined)])
        num_messages = len(self._commit.selected)

        use_subagent = False
        if self.options and self.options.get(Options.Subagents.Commit.Enabled) == "enabled":
            criterion = self.options.get(Options.Subagents.Commit.RoutingCriterion)
            threshold = self.options.get(Options.Subagents.Commit.RoutingThreshold)
            if criterion == "tokens":
                use_subagent = approx_tokens >= threshold
            else:
                use_subagent = num_messages >= threshold

        instructions_note = ""
        if instructions:
            instructions_note = f"\n\nUser provided these additional instructions for the commit:\n{instructions}"

        if use_subagent:
            self._agent_session.add_system_notification(
                f"User selected {num_messages} message(s) for commit "
                f"(~{approx_tokens} tokens). Use commit_invoke_subagent to delegate "
                "knowledge entry extraction, then present the proposal to the user."
                + instructions_note
            )
        else:
            self._agent_session.add_system_notification(
                f"User selected {num_messages} message(s) for commit "
                f"(~{approx_tokens} tokens). Use commit_show_selected_messages and "
                "commit_proposal_create to draft entries directly, then present "
                "the proposal to the user."
                + instructions_note
            )
        self._start_agent()
            

    def _commit_move_cursor(self, delta: int) -> None:
        """Move the commit-mode cursor highlight."""
        if not self._commit.selectable:
            return

        new_index = self._commit.cursor + delta
        if new_index < 0 or new_index >= len(self._commit.selectable):
            return

        # Remove cursor highlight from the old message, update the index, and add it to the new message.
        if 0 <= self._commit.cursor < len(self._commit.selectable):
            self._commit.selectable[self._commit.cursor].remove_class("--commit-cursor")
        self._commit.cursor = new_index

        target = self._commit.selectable[self._commit.cursor]
        target.add_class("--commit-cursor")
        target.scroll_visible()

    def on_key(self, event) -> None:
        # Remark: we only capture key events for commit mode, so we don't interfere with normal input,
        # command palette navigation, etc.
        if not self._commit.active:
            return

        # Stop propagation and prevent default behavior for all keys in commit mode,
        # since we're using them for navigation and selection.
        event.stop()
        event.prevent_default()

        key = event.key
        if key == "up":
            if self._commit.cursor > 0:
                self._commit_move_cursor(-1)

        elif key == "down":
            if self._commit.cursor < len(self._commit.selectable) - 1:
                self._commit_move_cursor(1)

        elif key == "space":
            msg = self._commit.selectable[self._commit.cursor]
            checkbox = msg.query_one(".commit-checkbox")

            if self._commit.cursor in self._commit.selected:
                # Deselect the current message.
                self._commit.selected.discard(self._commit.cursor)
                msg.remove_class("--commit-selected")

                if checkbox:
                    checkbox.update("☐")
            else:
                # Select, and move the cursor to the next message if possible.
                self._commit.selected.add(self._commit.cursor)
                msg.add_class("--commit-selected")

                if checkbox:
                    checkbox.update("☑")

                if self._commit.cursor < len(self._commit.selectable) - 1:
                    self._commit_move_cursor(1)

        elif key == "ctrl+j":
            # Confirm selection and exit commit mode.
            self.confirm_commit_selection()

        elif key == "escape":
            # Cancel selection and exit commit mode.
            self._commit.selected.clear()
            self.confirm_commit_selection()
