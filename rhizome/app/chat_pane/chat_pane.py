"""ChatPaneModel — steps 1–3 of the chat-pane MVVM rewrite.

Steps 1+2 cover the feed + commands; step 3 adds an ``AgentSession`` instance, held but unused. No
worker, no streaming, no harness yet — this is just the bootstrap seam.

Out of scope: starting/cancelling agent runs, sub-VMs in the feed, status-bar projection, shell ``!``
commands, agent gating of commands, the agent-busy half of the mode-transition matrix.
"""

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Literal, cast

import rich_click as click
from langchain_core.messages import HumanMessage
from langchain_core.messages.utils import count_tokens_approximately
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from rhizome.agent.session import AgentSession, get_agent_kwargs
from rhizome.db import Topic
from rhizome.db.operations import get_topic
from rhizome.resources.manager import ResourceManager
from rhizome.app.resource_viewer import ResourceViewerModel
from rhizome.app.command_registry import CommandRegistry
from rhizome.app.options import Options
from rhizome.tui.types import Mode, Role

from rhizome.app.browser.browser import BrowserModel
from rhizome.app.options_editor import OptionsEditorModel
from rhizome.app.model import ViewModelBase
from rhizome.app.chat_pane.messages.agent import AgentMessageModel
from rhizome.app.chat_pane.agent_stream_router import AgentStreamRouter
from rhizome.app.chat_pane.branch import BranchPointModel
from rhizome.app.chat_pane.chat_input import ChatInputModel
from rhizome.app.chat_pane.interrupts.user_choices import UserChoicesModel
from rhizome.app.chat_pane.command_palette import CommandPaletteModel
from rhizome.app.chat_pane.conversation_graph import ConversationGraph, ConversationGraphCursor, ConversationNode, NodeId
from rhizome.app.chat_pane.interrupts.base import InterruptModelBase
from rhizome.app.chat_pane.interrupts.test import TestInterruptModel
from rhizome.app.chat_pane.interrupts.multi_choices import MultiUserChoicesModel
from rhizome.app.chat_pane.interrupts.sql import SqlConfirmationModel
from rhizome.app.chat_pane.interrupts.warning import WarningUserChoicesModel
from rhizome.app.chat_pane.interrupts.flashcard_review import FlashcardReviewInterruptModel
from rhizome.app.chat_pane.interrupts.commit_proposal import CommitProposalInterruptModel
from rhizome.app.chat_pane.interrupts.flashcard_proposal import FlashcardProposalInterruptModel
from rhizome.app.commit_proposal import Entry, EntryType
from rhizome.app.flashcard_proposal import Flashcard
from rhizome.app.chat_pane.messages.shell import ShellCommandModel
from rhizome.app.chat_pane.messages.static import ChatMessageModel
from rhizome.app.chat_pane.status import StatusBarModel
from rhizome.app.chat_pane.thinking import ThinkingIndicatorModel
from rhizome.app.chat_pane.welcome_message import WelcomeMessageModel
from rhizome.app.chat_pane.messages.tool import ToolMessageModel


FeedEntry = (
    ChatMessageModel
    | AgentMessageModel
    | ToolMessageModel
    | ThinkingIndicatorModel
    | WelcomeMessageModel
    | InterruptModelBase
    | ShellCommandModel
    | BranchPointModel
    | BrowserModel
    | OptionsEditorModel
)


@dataclass
class FeedItem:
    """Wraps a feed entry with a stable, monotonically-assigned id.

    The id is the canonical handle for feed mutations (remove, future replace/ping). Position is not
    identity: a feed item's index can shift as later code adds out-of-band operations, so consumers
    that need to address a specific item must hold its id rather than its index.
    """
    id: int
    entry: FeedEntry


@dataclass
class ChatPaneConversationNode(ConversationNode[FeedItem]):
    """ConversationNode subclass attaching chat-pane-specific per-branch state.

    Beyond the generic feed/name/is_open, each branch carries the runtime objects that drive
    streaming on that branch:

    - ``agent_session``: the LangGraph-backed session for this branch (one per open leaf).
    - ``agent_task``: handle for the worker running ``_run_agent_turn`` against this branch.
      ``None`` ⟺ no in-flight turn on this branch. Drives ``agent_busy`` for the branch.
    - ``current_router``: ``AgentStreamRouter`` for the in-flight turn, mirroring ``agent_task``.
    - ``pending_interrupt``: interrupt VM awaiting user input on this branch, if any. Derives
      the chat input's enabled/hint state when the cursor sits on this branch.
    - ``last_visited_child``: most-recently-traversed child of this node. Recorded every time the
      cursor moves through the node and used by ``swap_sibling`` / ``descend_into`` to restore
      the previously-visited descendant chain — e.g. after swapping away from branch B (where
      the cursor was at (A, B, E)) and back again, the cursor lands on (A, B, E) rather than
      stopping at (A, B). ``None`` until the node has been traversed; chasing terminates at
      ``None`` or when the recorded child is no longer a valid child of this node.

    All fields default-initialize; the graph constructs nodes via ``cls(id=..., name=...)`` and
    leaves every subclass-specific field at its default, populated lazily by the chat pane as
    the branch transitions through bootstrap / fork / turn / interrupt / etc.
    """
    agent_session: AgentSession | None = None
    agent_task: object | None = None
    current_router: AgentStreamRouter | None = None
    pending_interrupt: InterruptModelBase | None = None
    last_visited_child: NodeId | None = None


_DEFAULT_HINT = "Type a message or /command ..."
_INTERRUPT_HINT = "Resolve the prompt above to continue..."


class ChatPaneModel(ViewModelBase):

    # Slash commands that must wait for the agent to be idle. Anything not in this set is allowed to
    # dispatch mid-stream (mode toggles, echo, test-* helpers, etc.). Shell `!` commands and free-text
    # chat are gated separately in ``_on_input_submitted``.
    _AGENT_GATED_COMMANDS: frozenset[str] = frozenset({"commit", "branch"})

    class State(Enum):
        """Coarse VM state. Most of the public API asserts ``state == CONVERSATION``; the COMMIT
        branch is entered via ``enter_commit_mode`` and exited via ``exit_commit_mode`` or
        ``submit_commit_payload``.
        """
        CONVERSATION = "conversation"
        COMMIT = "commit"

    class Callbacks(Enum):
        FEED_APPEND = "feed_append"
        FEED_REMOVE = "feed_remove"
        FEED_CLEAR = "feed_clear"
        FEED_REPLACED = "feed_replaced"
        TAB_RENAME = "tab_rename"
        NOTIFY = "notify"

    class NotifyAction(Enum):
        """Actions the VM asks the parent view to perform. The VM specifies the action; the view
        owns the concrete user-facing presentation (message text, severity, timeout, routing to a
        sibling pane, etc.). Use this for anything the chat-pane VM can't accomplish directly —
        eventually things like "open logs pane" once an app-level VM exists.
        """
        HINT_HIGHER_VERBOSITY = "hint_higher_verbosity"
        AGENT_BUSY = "agent_busy"
        DESCEND_REQUIRED = "descend_required"
        QUIT = "quit"
        NEW_TAB = "new_tab"
        CLOSE_TAB = "close_tab"
        OPEN_LOGS = "open_logs"
        TOGGLE_RESOURCE_VIEWER = "toggle_resource_viewer"

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        *,
        show_welcome: bool = False,
    ) -> None:
        super().__init__()

        # Whether to seed the feed with a welcome banner on bootstrap. Cleared once shown so a
        # view re-mount (tab churn) doesn't append a second banner.
        self._show_welcome = show_welcome

        self._feed_append = self._make_group(ChatPaneModel.Callbacks.FEED_APPEND)
        self._feed_remove = self._make_group(ChatPaneModel.Callbacks.FEED_REMOVE)
        self._feed_clear = self._make_group(ChatPaneModel.Callbacks.FEED_CLEAR)
        self._feed_replaced = self._make_group(ChatPaneModel.Callbacks.FEED_REPLACED)
        self._tab_rename = self._make_group(ChatPaneModel.Callbacks.TAB_RENAME)
        self._notify = self._make_group(ChatPaneModel.Callbacks.NOTIFY)

        # Conversation feed lives in a ConversationGraph parameterized over ``ChatPaneConversationNode``
        # so every node carries chat-pane-specific per-branch state (``agent_session`` for now;
        # task/router/interrupt to follow). ``self.feed`` continues to expose the current cursor
        # leaf's feed list directly. The ``node_cls`` lets ``_new_node`` construct the right
        # subclass without the graph itself knowing chat-pane concerns.
        self._conversation: ConversationGraph[FeedItem] = ConversationGraph(
            root_name="main",
            node_cls=ChatPaneConversationNode,
        )
        self._cursor: ConversationGraphCursor = self._conversation.cursor_at_root()
        self._next_feed_id: int = 0

        self.state: ChatPaneModel.State = ChatPaneModel.State.CONVERSATION

        # Commit-mode working set, valid only while ``state == COMMIT``. ``_commit_selectable`` is
        # the snapshot of learn-mode AgentMessageVMs in feed order at enter time; ``_commit_cursor``
        # is the index of the highlighted entry. Reset by ``exit_commit_mode`` /
        # ``submit_commit_payload``.
        self._commit_selectable: list[AgentMessageModel] = []
        self._commit_cursor: int = 0

        self.session_mode: Mode = Mode.IDLE

        # Active topic + path from the topic tree root. Mutated via set_topic / clear_topic; surfaced
        # by the view in the status bar.
        self.active_topic: Topic | None = None
        self.topic_path: list[str] = []

        self.command_palette = CommandPaletteModel()
        self._command_registry = CommandRegistry()
        self._register_commands()
        self.command_palette.set_commands(self._registry_rows())

        # Input sub-VM owns buffer/enabled/hint/history + holds the shared palette so the input view
        # never reaches into the pane to filter, navigate, or decide tab-completion vs submit. The pane
        # subscribes to ``submitted`` to dispatch chat-vs-slash + agent-busy gating.
        self.chat_input = ChatInputModel(self.command_palette, default_hint=_DEFAULT_HINT)
        self.chat_input.subscribe(self.chat_input.submitted, self._on_input_submitted)

        # Status-bar sub-VM. Projection of mode / topic_path (from this VM), token_usage + model_name
        # (from the agent session), and verbosity (from app.options). Pane mutates it through setters;
        # the view subscribes to its own dirty so token updates don't repaint the rest of the pane.
        self.status_bar = StatusBarModel()
        self._options: Options | None = None

        # Agent plumbing — instantiated on bootstrap (after the view has access to app.options). Held
        # but unused at step 3.
        self._session_factory = session_factory
        self.resource_manager: ResourceManager | None = (
            ResourceManager(session_factory=session_factory) if session_factory else None
        )

        # Side-panel resource viewer. Owned here (not by the view) so its load/link/cursor state
        # survives toggling the panel open and closed — the view mounts/unmounts the widget against
        # this persistent VM. Shares the agent's ``resource_manager`` so resources loaded in the
        # panel reach the agent's context. ``None`` without a session (test / headless), mirroring
        # ``resource_manager``; the ``/resources`` command surfaces a message in that case.
        self.resource_viewer: ResourceViewerModel | None = (
            ResourceViewerModel(session_factory, manager=self.resource_manager)
            if session_factory
            else None
        )
        # AgentSession now lives on the ChatPaneConversationNode itself — one per open leaf,
        # populated by ``bootstrap_agent_session`` (root leaf) and ``branch()`` (continuation + new
        # branch via fork). The ``agent_session`` property resolves to the session at the current
        # cursor's head, so call sites that say ``self.agent_session`` always reach the session for
        # whichever branch is currently displayed.

        # Per-branch agent runtime — ``agent_task``, ``current_router``, ``pending_interrupt``
        # live on each ``ChatPaneConversationNode``. ``agent_busy`` reads the current cursor's
        # node, so multiple branches can stream concurrently without blocking each other.

        # Scheduler hook: the View overrides this on mount to use Textual's ``run_worker`` (lifecycle
        # binds to the widget, errors surface via the app, DevTools sees the worker). Default plain
        # ``create_task`` keeps headless/test usage working.
        self._schedule_worker: Callable[[Coroutine[Any, Any, Any]], object] = asyncio.create_task

    # ------------------------------------------------------------------
    # Callback group accessors
    # ------------------------------------------------------------------

    @property
    def feed_append(self):
        return self._feed_append

    @property
    def feed_remove(self):
        return self._feed_remove

    @property
    def feed_clear(self):
        return self._feed_clear

    @property
    def feed_replaced(self):
        """Fired when the cursor moves and the visible feed is wholesale replaced.

        The view diffs ``self.feed`` (the new visible projection) against its currently-mounted
        widget id set, unmounts what's no longer visible, and mounts what's newly visible. Because
        any two cursor paths share a prefix corresponding to their longest common ancestor chain,
        the delta is always a tail change — new mounts append at the end, no positional insertion
        needed.
        """
        return self._feed_replaced

    @property
    def tab_rename(self):
        return self._tab_rename

    @property
    def notify(self):
        return self._notify

    @property
    def command_registry(self) -> CommandRegistry:
        return self._command_registry

    @property
    def agent_session(self) -> AgentSession | None:
        """The AgentSession bound to the cursor's current leaf, or ``None`` before bootstrap.

        Reading is cursor-keyed: navigating to a different leaf changes which session this resolves
        to without moving any state around. Writes happen directly on the node (see
        ``bootstrap_agent_session`` and ``branch()``).
        """
        return self._node(self._cursor.head).agent_session

    def _node(self, node_id: NodeId) -> ChatPaneConversationNode:
        """Typed accessor for a chat-pane node. The graph is constructed with
        ``node_cls=ChatPaneConversationNode`` so every node is one at runtime; this just narrows
        the type so callers can access pane-specific attributes without scattering ``cast()``.
        """
        return cast(ChatPaneConversationNode, self._conversation.node(node_id))

    def _all_sessions(self) -> list[AgentSession]:
        """Every AgentSession currently attached to a node in the graph.

        Used by mode/options fan-out paths to propagate shared state to all branches. Closed nodes
        whose sessions have been forwarded to a continuation/new branch have their slot cleared by
        ``branch()``, so each session appears here at most once.
        """
        return [
            s for nid in self._conversation
            if (s := self._node(nid).agent_session) is not None
        ]

    @property
    def feed(self) -> list[FeedItem]:
        """The current cursor leaf's feed list — the live underlying list, not a copy.

        Returning the actual list (rather than a snapshot) preserves the in-place mutation
        patterns (``feed.append``, ``feed.clear``, ``del feed[i]``) used by ``_append_feed``,
        ``_remove_feed``, and ``clear_feed``. Reads that semantically want "everything visible to
        the user across the current cursor path" should use :attr:`visible_feed` instead.
        """
        return self._conversation.node(self._cursor.head).feed

    @property
    def visible_feed(self) -> list[FeedItem]:
        """Fresh list of every FeedItem on the cursor path, in render order.

        This is what the view renders — concat of node feeds along the cursor path, which after a
        /branch includes the parent's feed plus the descended child's feed. Re-computed on each
        access; not stable across cursor moves.
        """
        return self._conversation.visible_feed(self._cursor)

    def visible_feed_by_depth(self) -> list[tuple[NodeId, list[FeedItem]]]:
        """Visible feed grouped by depth — one ``(node_id, items)`` entry per node on the cursor
        path, in root-to-leaf order. Index ``i`` is the depth-``i`` group (root is depth 0).

        The flat concatenation of all groups' items equals :attr:`visible_feed`. The view uses
        this to mount each item into a per-depth container (one nested wrapper per level), which
        is how the left-side depth rules get their per-y-position depth without any post-layout
        coordinate gymnastics.
        """
        return [
            (node_id, list(self._conversation.node(node_id).feed))
            for node_id in self._cursor.path
        ]

    # ------------------------------------------------------------------
    # Feed-wide navigation (ctrl+up / ctrl+down)
    # ------------------------------------------------------------------

    def navigate_feed(self, direction: int, current_id: int | None) -> None:
        """Move focus across navigable feed entries.

        Filters ``visible_feed`` by ``entry.is_navigable`` and walks the resulting list. ``direction``
        is ``-1`` (up) or ``+1`` (down). ``current_id`` is the ``FeedItem.id`` containing focus, or
        ``None`` when focus is on the chat input (or elsewhere outside the feed).

        Semantics:
        - From the input (``current_id is None``): ctrl+up jumps to the bottom-most navigable;
          ctrl+down jumps to the top-most.
        - From a navigable entry: step by ``direction``. Ctrl+up clamps at the top (there is
          nothing useful above the feed). Ctrl+down past the last navigable hands focus back to
          the chat input — completing a single-key round-trip out of the feed.
        - With no navigable entries: no-op.
        """
        # Every feed entry is a VM, so ``is_navigable`` (defaulting to False on ``ViewModelBase``) is
        # always present — only interrupts flip it on.
        navigables = [item for item in self.visible_feed if item.entry.is_navigable]
        if not navigables:
            return

        current_idx: int | None = None
        if current_id is not None:
            for i, item in enumerate(navigables):
                if item.id == current_id:
                    current_idx = i
                    break

        if current_idx is None:
            new_idx = len(navigables) - 1 if direction < 0 else 0
        else:
            new_idx = current_idx + direction
            if new_idx >= len(navigables):
                # Past the bottom: fall through to the chat input.
                self.chat_input.request_focus()
                return
            new_idx = max(0, new_idx)

        target = navigables[new_idx].entry
        target.request_focus()

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------

    def set_worker_scheduler(self, scheduler: Callable[[Coroutine[Any, Any, Any]], object]) -> None:
        """Inject a scheduler for spawning long-running coroutines (agent turn, async command
        handlers). Called by the View on mount with Textual's ``run_worker``; tests / headless callers
        can leave the default ``asyncio.create_task``.
        """
        self._schedule_worker = scheduler

    def bootstrap_agent_session(self, app_options: Options, *, debug: bool = False) -> None:
        """Construct ``self.agent_session`` from app options. Idempotent: a second call is a no-op.
        Caller is the view's ``on_mount`` because the provider/model values live on Textual's
        ``app.options``.
        """
        if self.agent_session is not None:
            return
        if self._session_factory is None:
            return

        self._options = app_options

        provider = app_options.get(Options.Agent.Provider)
        model_name = app_options.get(Options.Agent.Model)
        agent_kwargs = get_agent_kwargs(app_options)

        self._node(self._cursor.head).agent_session = AgentSession(
            self._session_factory,
            chat_pane=self,
            resource_manager=self.resource_manager,
            provider=provider,
            model_name=model_name,
            agent_kwargs=agent_kwargs,
            on_token_usage_changed=self._on_token_usage_changed,
            debug=debug,
        )

        # Seed status-bar fields that come from the agent session / options.
        self.status_bar.set_model_name(self.agent_session._model_name or "")
        self.status_bar.set_verbosity(app_options.get(Options.Agent.AnswerVerbosity))
        app_options.subscribe(Options.Agent.AnswerVerbosity, self._on_verbosity_changed)
        # Options changes (provider/model/agent_kwargs) are shared across all branches: fan out
        # post-update to every per-leaf AgentSession so each one rebuilds in lockstep. Bootstrap is
        # idempotent (early-returns above) so this subscription is wired exactly once.
        app_options.subscribe_post_update(self._on_options_post_update)

    def bootstrap_welcome(self, app_options: Options) -> None:
        """Seed a fresh feed with the welcome banner when the pane was constructed with
        ``show_welcome``. Called from the view's ``on_mount`` (the user name lives on app options).
        Independent of the agent session, so it works even in session-less / headless setups. Fires
        at most once — the flag is cleared on first append.
        """
        if not self._show_welcome:
            return
        self._show_welcome = False
        self._append_feed(WelcomeMessageModel(user_name=app_options.get(Options.UserName)))

    def _on_token_usage_changed(self, session: AgentSession) -> None:
        """Route a session's token-usage update to the status bar only if that session is the
        one currently displayed. Background-branch streams update their own sessions' counters
        without disturbing the visible status bar; navigation to a different branch is what
        triggers the visible refresh, via ``_sync_navigation_state``.
        """
        if session is self.agent_session:
            self.status_bar.set_token_usage(session.token_usage)

    async def _on_verbosity_changed(self, _old, new) -> None:
        self.status_bar.set_verbosity(new)

    async def _on_options_post_update(self, options: Options) -> None:
        """Fan options changes out to every per-leaf AgentSession.

        Each session decides for itself whether the change warrants a rebuild (provider/model/kwargs
        diff). Sessions that don't need to rebuild are no-ops, so an empty broadcast is harmless.
        """
        for session in self._all_sessions():
            await session.on_options_post_update(options)

    # ------------------------------------------------------------------
    # Feed
    # ------------------------------------------------------------------

    def _append_feed(
        self,
        entry: FeedEntry,
        *,
        cursor: ConversationGraphCursor | None = None,
    ) -> FeedItem:
        """Wrap ``entry`` in a ``FeedItem`` with a fresh id, append it, and emit ``feed_append``.

        By default appends to the *current* cursor's leaf feed. Callers that need to pin a target
        branch — most notably ``AgentStreamRouter``, which is constructed at the start of a turn
        and must keep routing into that branch even if the user navigates away mid-stream — pass
        an explicit ``cursor``. Item ids are assigned from a single VM-wide counter so they stay
        globally unique regardless of which node holds the item; the view's ``visible_feed``
        lookup will simply miss off-path items until the user navigates back to that branch.
        """
        item = FeedItem(id=self._next_feed_id, entry=entry)
        self._next_feed_id += 1
        target = cursor if cursor is not None else self._cursor
        self._conversation.node(target.head).feed.append(item)
        self.emit(self.feed_append, item.id)
        return item

    def _remove_feed(self, item_id: int, *, cursor: ConversationGraphCursor | None = None) -> None:
        """Remove a feed item by id from a target node's feed. No-op if not found.

        Mirrors :meth:`_append_feed`'s cursor parameter — pinned callers pass the cursor they
        appended through, so removal looks in the right place regardless of where the user has
        navigated.
        """
        target = cursor if cursor is not None else self._cursor
        feed = self._conversation.node(target.head).feed
        for i, item in enumerate(feed):
            if item.id == item_id:
                del feed[i]
                self.emit(self.feed_remove, item_id)
                return

    def append_message(
        self,
        msg: ChatMessageModel,
        *,
        include_in_agent_context: bool = True,
        cursor: ConversationGraphCursor | None = None,
    ) -> None:
        """Append a message to the feed, applying the consecutive-system dedup rule.

        If the previous feed entry is a system message with identical content, we suppress the append;
        otherwise the message is appended and ``feed_append`` fires with the new item id. Does **not**
        close any open agent turn — see "Feed ordering rules" above ``open_agent_turn``.

        When ``include_in_agent_context`` is True (the default), USER messages are pushed to the agent
        via ``add_human_message`` and SYSTEM messages via ``add_system_notification`` so they land in
        the conversation history on the next stream. Other roles (ERROR, etc.) are never forwarded —
        they're UI-side noise the agent shouldn't react to. Callers wanting feed-only mutation (test
        commands, UI echoes, legacy ``ui_only=True`` cases) pass ``include_in_agent_context=False``.

        ``cursor``: optional target branch, matching :meth:`_append_feed`'s semantics. The dedup
        peek uses *that* cursor's visible feed; the agent-context forwarding goes to *that* branch's
        session. Used by ``_run_agent_turn`` to route cancelled/error messages into the pinned
        branch even if the user has navigated away.
        """
        assert self.state == ChatPaneModel.State.CONVERSATION
        target = cursor if cursor is not None else self._cursor

        # Dedup peek uses the full visible feed of the target branch so a system message that
        # just arrived in a freshly descended branch can be suppressed against a tail-of-parent
        # identical message.
        visible = self._conversation.visible_feed(target)
        tail_entry = visible[-1].entry if visible else None
        if (
            msg.role == Role.SYSTEM
            and isinstance(tail_entry, ChatMessageModel)
            and tail_entry.role == Role.SYSTEM
            and tail_entry.content == msg.content
        ):
            # TODO: ping the existing entry. Will likely flow through a per-entry dirty once
            # ChatMessageModel owns its own emit channel.
            return

        self._append_feed(msg, cursor=target)

        if include_in_agent_context:
            target_session = self._node(target.head).agent_session
            if target_session is not None:
                if msg.role == Role.USER:
                    target_session.add_human_message(msg.content)
                elif msg.role == Role.SYSTEM:
                    target_session.add_system_notification(msg.content)

    def clear_feed(self) -> None:
        # TODO: with per-branch agent state, ``/clear`` only inspects/abandons the *current* branch.
        # It should also check whether any *other* branch has active work (an in-flight turn or a
        # pending interrupt) and either refuse or confirm with the user before proceeding — leaving
        # the cleared current branch sitting next to a running other-branch task would be confusing
        # and could lose context the user didn't mean to drop.
        assert self.state == ChatPaneModel.State.CONVERSATION
        current = self._node(self._cursor.head)
        if not self.feed and current.current_router is None:
            return

        self.cancel_agent_turn()

        if self.feed:
            self.feed.clear()
            self.emit(self.feed_clear)

        # The pane state may have changed (agent_busy flipped to False if we cancelled a turn);
        # repaint so the input reflects that.
        self.emit(self.dirty)

    def cancel_agent_turn(self, node_id: NodeId | None = None) -> None:
        """Tear down the in-flight agent turn on a branch (current branch by default).

        Public entry point for user-initiated cancellation (ctrl+c from the view) and the
        internal cleanup path from ``clear_feed``. Routes through the branch's
        ``AgentStreamRouter.close(cancelled=True)`` before cancelling the worker task — the
        router is the only thing that knows the thinking indicator's feed id and holds the
        open segment references, so cleanup has to go through it. Cancelling the task without
        closing the router first orphans the indicator: the task's own ``finally`` block bails
        out (it guards on ``current_router is router``, which we've cleared synchronously by
        then) so cleanup never runs.

        Order:
          1. ``router.close(cancelled=True)`` — pause + remove the thinking indicator. Skips
             the "(no response)" stub since the worker's ``CancelledError`` handler will post
             "(user cancelled)" once the coroutine unwinds.
          2. ``task.cancel()`` — kick the worker so it unwinds and posts its system message.

        ``agent_busy`` flips to False synchronously here (``agent_task`` cleared before the
        cancel reaches the worker) so the input re-enables on the same tick.
        """
        target_id = node_id if node_id is not None else self._cursor.head
        node = self._node(target_id)
        if node.current_router is None:
            return

        router = node.current_router
        node.current_router = None
        router.close(cancelled=True)

        if node.agent_task is not None:
            task = node.agent_task
            node.agent_task = None
            # ``agent_task`` is typed ``object`` to keep Textual's Worker out of this VM's surface,
            # but at runtime it's always cancellable (asyncio.Task or Textual Worker).
            task.cancel()  # type: ignore[attr-defined]

        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Feed ordering rules
    # ------------------------------------------------------------------
    #
    # The feed is append-only and addressed by ``FeedItem.id`` — **position is not identity**.
    # Agent-turn routing lives in ``AgentStreamRouter`` (see ``agent_stream_router.py``), one
    # instance per turn. ``_run_agent_turn`` constructs the router (which mounts the thinking
    # indicator), stores it on ``_current_router`` for the duration, and closes + discards it in
    # its finally block.
    # The router's stream callbacks route chunks/tool calls into chat/tool segments and repin the
    # indicator; ``present_interrupt`` reaches into ``_current_router`` to pause before awaiting
    # the user. ``agent_busy`` (worker task aliveness) is the single source of truth for "is the
    # agent running".


    # ------------------------------------------------------------------
    # Interrupts
    # ------------------------------------------------------------------

    async def present_interrupt(
        self,
        vm: InterruptModelBase,
        *,
        cursor: ConversationGraphCursor | None = None,
    ) -> Any:
        """Append an interrupt VM to the feed and await its resolution.

        Closes any currently-open agent turn (peek-tail). Disables the chat input + swaps in a
        contextual hint for the duration; restores both on resolve/cancel. Returns the resolved value,
        or ``None`` if cancelled.

        The interrupt VM stays in the feed after resolution as an inert record — the View dims it but
        doesn't remove it, so the conversation history reflects what was chosen.

        ``cursor``: optional pinned target for the append, matching :meth:`_append_feed`'s
        semantics. Router-driven interrupts pass the turn's pinned cursor so the interrupt widget
        lands in the branch where the turn was launched even if the user has navigated away
        mid-stream; if the user is elsewhere they won't see it until they navigate back, at which
        point the view's diff mounts it and the future awaits user input as usual.
        """
        assert self.state == ChatPaneModel.State.CONVERSATION

        # TODO: It is up in the air whether or not we want to pause the current router to post _any_ interrupt
        # or let the router itself pause when posting it's own interrupt (through the on_interrupt handler).
        #
        # The behaviour if we pause here is as follows: while the agent is responding, if a separate interrupt
        # is posted to the feed, then the .pause() call will close the current agent message, then append the
        # interrupt to the feed, then the next agent response chunk will spawn a _new_ agent message _below_
        # the interrupt. In other words, calling .pause() here means exogenous interrupts "break agent messages
        # in half".

        # router = self._node(target_cursor.head).current_router
        # if router is not None:
        #     router.pause()

        target_cursor = cursor if cursor is not None else self._cursor
        target_node = self._node(target_cursor.head)
        target_node.pending_interrupt = vm
        self._append_feed(vm, cursor=target_cursor)

        # Chat-input state is fully derived from the *current* branch's pending interrupt — no
        # snapshot-and-restore needed. The sync helper checks ``self._cursor.head`` (not the
        # pinned target), so an interrupt on a background branch leaves the visible input alone
        # and the user only sees the lockout if/when they navigate to that branch.
        self._sync_chat_input_to_cursor()

        try:
            return await vm.future()
        except asyncio.CancelledError:
            return None
        finally:
            target_node.pending_interrupt = None
            self._sync_chat_input_to_cursor()

    # ------------------------------------------------------------------
    # Agent run lifecycle
    # ------------------------------------------------------------------
    #
    # ``start_agent_run`` appends the user message, queues it on the AgentSession, and spawns
    # ``_run_agent_turn`` via the injected ``_schedule_worker``. ``_run_agent_turn`` drives
    # ``AgentSession.stream`` with VM-side callbacks that route into the open AgentMessage VM. Under
    # Textual, the View injects ``self.run_worker`` so the worker's lifecycle binds to the widget.

    @property
    def agent_busy(self) -> bool:
        """An agent turn is in flight on the *current branch* (cursor-keyed).

        Concurrent turns on other branches don't make the current branch busy. Use
        :meth:`is_branch_busy` for an arbitrary node.
        """
        return self._node(self._cursor.head).agent_task is not None

    def is_branch_busy(self, node_id: NodeId) -> bool:
        """True iff the branch at ``node_id`` has an in-flight agent turn."""
        return self._node(node_id).agent_task is not None

    def start_agent_run(self, user_text: str) -> None:
        """Append the user message and kick off an agent turn. No-op if a run is already in flight
        (real queueing comes with the feed-queue)."""
        assert self.state == ChatPaneModel.State.CONVERSATION
        if self.agent_busy:
            return

        if self.agent_session is None:
            self.append_message(ChatMessageModel(role=Role.ERROR, content="Agent session not bootstrapped."))
            return

        self.append_message(ChatMessageModel(role=Role.USER, content=user_text))
        self._start_agent_turn()

    def _start_agent_turn(self) -> None:
        """Spawn ``_run_agent_turn`` on the worker scheduler. Used by ``start_agent_run`` (which
        prefixes a USER message) and by ``submit_commit_payload`` (which kicks off agent-only,
        driven by an injected commit payload + system notification, with no USER message).

        Caller preconditions: ``state == CONVERSATION``, current branch's ``agent_session``
        non-None, current branch not busy. The turn pins itself to the cursor's leaf at start
        time: the router (and its cursor snapshot) is constructed synchronously here, then both
        the router and the scheduled task are stored on the pinned ``ChatPaneConversationNode``.
        Mid-turn navigation can't redirect output, and concurrent turns on other branches each
        live on their own node.
        """
        if self.agent_busy:
            return
        pinned_node = self._node(self._cursor.head)
        if pinned_node.agent_session is None:
            return
        # Closed branch points (re-visited via ascend / non-leaf navigation) carry a frozen
        # snapshot session as a fork source — they are not streamable. Streaming one would
        # mutate it and break its role as the immutable seed for future ``/branch`` calls.
        if not self._conversation.node(self._cursor.head).is_open:
            return
        router = AgentStreamRouter(self)
        pinned_node.current_router = router
        pinned_node.agent_task = self._schedule_worker(self._run_agent_turn(pinned_node, router))
        self.emit(self.dirty)


    async def _run_agent_turn(
        self,
        pinned_node: ChatPaneConversationNode,
        router: AgentStreamRouter,
    ) -> None:
        """Run an agent turn pinned to ``pinned_node``.

        ``router`` was constructed synchronously in ``_start_agent_turn`` so its cursor pin
        matches the launch leaf. All feed mutations (chat segments, tool calls, thinking
        indicator, the cancelled/error system message below) target that pinned cursor; the
        user can navigate to other branches mid-turn without redirecting output.
        """
        session = pinned_node.agent_session
        assert session is not None
        pinned_cursor = router._cursor

        try:
            await session.stream(
                mode=self.session_mode.value,
                on_message=router.on_message,
                on_update=router.on_update,
                on_interrupt=router.on_interrupt,
                cursor=pinned_cursor,
            )

        except asyncio.CancelledError:
            self.append_message(
                ChatMessageModel(role=Role.SYSTEM, content="(user cancelled)"),
                cursor=pinned_cursor,
            )
            raise

        except Exception as exc:  # noqa: BLE001 — surface stream errors as ERROR messages
            if pinned_node.current_router is router:
                self.append_message(
                    ChatMessageModel(role=Role.ERROR, content=f"Agent error: {exc}"),
                    cursor=pinned_cursor,
                )

        finally:
            if pinned_node.current_router is router:
                router.close()
                pinned_node.current_router = None
                pinned_node.agent_task = None
                self.emit(self.dirty)


    # ------------------------------------------------------------------
    # Session mode
    # ------------------------------------------------------------------

    async def cycle_mode(self) -> None:
        """Advance through IDLE → LEARN → REVIEW → IDLE. Silent — the binding's intent is a quick
        cycle, not a chat-visible mode change.
        """
        assert self.state == ChatPaneModel.State.CONVERSATION
        cycle = {Mode.IDLE: Mode.LEARN, Mode.LEARN: Mode.REVIEW, Mode.REVIEW: Mode.IDLE}
        await self.set_mode(cycle[self.session_mode], silent=True)

    async def cycle_verbosity(self) -> None:
        """Advance through ``Options.Agent.AnswerVerbosity.choices``. The existing options
        subscription updates the status bar VM."""
        if self._options is None:
            return
        choices = Options.Agent.AnswerVerbosity.choices
        current = self._options.get(Options.Agent.AnswerVerbosity)
        idx = choices.index(current) if current in choices else 0
        new_value = choices[(idx + 1) % len(choices)]
        await self._options.set(Options.Agent.AnswerVerbosity, new_value)
        await self._options.post_update()

    def _set_session_mode(self, mode: Mode) -> None:
        """Assign session_mode and forward to the status-bar VM in one place."""
        self.session_mode = mode
        self.status_bar.set_mode(mode.value)

    async def set_mode(
        self, mode: Mode, *, silent: bool = False, source: Literal["user", "agent"] = "user",
    ) -> None:
        """Set the session mode.

        Args:
            mode: target mode.
            silent: suppress the chat system message (shift+tab cycling, agent-initiated changes).
                Forced True when source=="agent".
            source: ``"user"`` (UI-initiated — queues a pending change on the agent middleware so
                graph state catches up on the next model call) or ``"agent"`` (tool-initiated — graph
                state is updated directly via ``Command``).
        """
        assert self.state == ChatPaneModel.State.CONVERSATION
        if source == "agent":
            assert self.agent_busy
            silent = True

        if self.session_mode == mode:
            if not silent:
                self.append_message(
                    ChatMessageModel(role=Role.SYSTEM, content=f"Already in {mode.value} mode.")
                )
            return

        message = "Returned to idle mode." if mode == Mode.IDLE else f"Entered {mode.value} mode."

        # TODO: We really need to clean this up now that we have multiple agent sessions

        if source == "agent":
            self._set_session_mode(mode)
            self.emit(self.dirty)
            # User-initiated mode set while the agent was running is superseded by the agent's tool
            # call.
            if self.agent_session is not None:
                await self.agent_session._mode_middleware.clear_pending_user_mode()
            return

        # source == "user". Mode is shared state across all branches, so the queued change and the
        # accompanying notification fan out to every per-leaf AgentSession — each branch's history
        # records the same shift the moment it next streams.
        if self.agent_busy:
            self._set_session_mode(mode)
            for session in self._all_sessions():
                await session.set_pending_user_mode(mode.value)
            if not silent:
                # Feed-only: set_pending_user_mode handles the agent side when the queue drains.
                self.append_message(
                    ChatMessageModel(role=Role.SYSTEM, content=message, mode=mode),
                    include_in_agent_context=False,
                )
        else:
            self._set_session_mode(mode)
            if silent:
                for session in self._all_sessions():
                    session.add_system_notification(message)
            else:
                self.append_message(ChatMessageModel(role=Role.SYSTEM, content=message, mode=mode))

        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Topic / tab / verbosity (agent-tool-facing API)
    # ------------------------------------------------------------------

    def clear_topic(self) -> None:
        """Drop the active topic. View re-renders the status bar via dirty."""
        if self.active_topic is None and not self.topic_path:
            return
        self.active_topic = None
        self.topic_path = []
        self.status_bar.set_topic_path([])
        self.emit(self.dirty)

    async def set_topic(self, topic_id: int) -> bool:
        """Resolve ``topic_id`` and set it as the active topic. Walks parents to build ``topic_path``.
        Returns False if the topic does not exist (state is left unchanged).
        """
        if self._session_factory is None:
            return False

        async with self._session_factory() as session:
            topic = await get_topic(session, topic_id)
            if topic is None:
                return False
            path: list[str] = [topic.name]
            current = topic
            while current.parent_id is not None:
                current = await get_topic(session, current.parent_id)
                if current is None:
                    break
                path.append(current.name)
            path.reverse()

        self.active_topic = topic
        self.topic_path = path
        self.status_bar.set_topic_path(path)
        self.emit(self.dirty)
        return True

    async def set_tab_name(self, name: str) -> None:
        """Rename the active tab. The VM doesn't own tab state — emits through the ``tab_rename``
        callback group for the view (or a Tabs-level VM) to apply.
        """
        new_name = name.strip()
        if not new_name:
            return
        self.emit(self.tab_rename, new_name)

    def hint_higher_verbosity(self) -> None:
        """Hint to the user that a higher verbosity setting may help. The view decides how to
        present the cue.
        """
        self.emit(self.notify, ChatPaneModel.NotifyAction.HINT_HIGHER_VERBOSITY)

    # ------------------------------------------------------------------
    # Branching and navigation
    # ------------------------------------------------------------------

    def branch(self, *, branch_name: str | None = None) -> NodeId:
        """Branch from the cursor's current node and descend into the new branch.

        Two cases, both end with the cursor on the new branch:

        - **First branch at this node** (cursor on an open leaf): closes the leaf and creates a
          continuation child (inheriting the parent's name) plus the new branch. A new
          ``BranchPointModel`` is mounted on the parent's feed.
        - **Subsequent branch at a closed branch point** (cursor on a non-leaf): just adds
          another sibling to the existing children. The existing indicator is nudged dirty so
          it picks up the new child.

        AgentSession bookkeeping: the parent's session is kept as a *frozen snapshot* —
        never streamed again, but available as a fork source for additional siblings later.
        The continuation (if newly created) and the new branch each get their own fresh forks
        so they evolve independently without mutating the parent's snapshot.

        Invariant: open leaves hold streamable sessions; closed nodes may hold frozen snapshots
        (one per branch point) used as fork sources. ``_start_agent_turn`` and friends guard
        against trying to stream a snapshot via ``ConversationNode.is_open`` checks.

        Gated on ``not agent_busy`` (also enforced via ``_AGENT_GATED_COMMANDS`` for the slash-command
        path); asserted here to guard direct programmatic callers.
        """
        assert self.state == ChatPaneModel.State.CONVERSATION
        assert not self.agent_busy

        # Walk-up case: if the cursor sits on an open leaf with an empty feed and has a parent,
        # treat the /branch as "give me another sibling at the parent's level" instead of
        # "branch within this empty leaf". The natural UX when the user descended into a fresh
        # branch and changed their mind before typing anything: don't bury the new branch
        # one level deeper, hoist it up to the parent. The abandoned empty leaf stays in the
        # graph and remains reachable via the parent's indicator. Mutate the cursor in place
        # (no ``ascend`` call) so the view doesn't render an intermediate "at parent" state
        # before the descent into the new branch.
        if (
            len(self._cursor.path) >= 2
            and self._conversation.node(self._cursor.head).is_open
            and not self._node(self._cursor.head).feed
        ):
            self._cursor = ConversationGraphCursor(self._cursor.path[:-1])

        parent_id = self._cursor.head
        parent_node = self._node(parent_id)
        parent_was_leaf = self._conversation.node(parent_id).is_open
        parent_session = parent_node.agent_session

        if parent_was_leaf:
            # Mount the indicator into the parent's feed *before* graph.branch() closes the
            # parent — otherwise ``self.feed`` would resolve to the new branch's empty feed by
            # the time we appended. The indicator's ``children`` property is lazy, so reading
            # it at compose time (post-branch) returns the freshly-opened children.
            indicator = BranchPointModel(self._conversation, parent_id, self)
            self._append_feed(indicator)

        new_cursor, new_branch_id = self._conversation.branch(self._cursor, branch_name=branch_name)
        self._cursor = new_cursor

        if parent_session is not None:
            if parent_was_leaf:
                # Continuation was just created; give it its own fork so the parent's session
                # stays frozen.
                continuation_id = self._conversation.children(parent_id)[0]
                self._node(continuation_id).agent_session = parent_session.fork()
            self._node(new_branch_id).agent_session = parent_session.fork()

        if not parent_was_leaf:
            # Existing indicator on the closed parent's feed needs a nudge so the new child
            # name shows up. ``_sync_navigation_state`` below will also push the updated
            # selected_child, but that's equality-guarded against no-op moves, so the dirty
            # nudge here is what triggers the re-render for the children-list change.
            for item in self._conversation.node(parent_id).feed:
                if isinstance(item.entry, BranchPointModel):
                    item.entry.emit(item.entry.dirty)
                    break

        # Cursor moved (descended into the new branch). The visible-feed delta is at most the
        # newly-mounted indicator (already handled by feed_append in the leaf case) plus the
        # new branch's empty feed — no other widgets need mount/unmount, so no feed_replaced.
        self._record_visit(self._cursor)
        self._sync_navigation_state()
        return new_branch_id

    def branch_and_send(self, prompt: str) -> None:
        """Branch from the current node and send ``prompt`` as the first user message on the
        new branch.

        Used by ``/branch <prompt>``: the dispatch in ``_on_input_submitted`` peels off the raw
        post-name string and forwards it here, bypassing the click/shlex pipeline so the prompt
        can contain apostrophes, quotes, and other characters that would otherwise crash
        tokenization. The new branch is left unnamed — the indicator falls back to
        ``branch-{id}`` until the agent calls ``update_app_state(current_branch_name=...)``
        (prompted by the queued system notification below) to set a meaningful one.

        ``self.branch()`` already lands the cursor on the new branch (both leaf and non-leaf
        cases), so no extra navigation is needed before ``start_agent_run``.
        """
        self.branch()

        # Queue the UI-hidden rename instruction *before* ``start_agent_run`` so it's
        # guaranteed to be in the message queue by the time the worker drains it. (Doing it
        # after relies on ``start_agent_run`` being synchronous all the way through, which is
        # fragile — any future await between the worker spawn and the drain would race.) The
        # agent sees the instruction first, then the user prompt — which reads naturally as
        # "here's a side-channel directive, here's the actual user message".
        if self.agent_session is not None:
            self.agent_session.add_system_notification(
                "Call `update_app_state(current_branch_name=...)` to set a short "
                "descriptive name for this branch."
            )

        self.start_agent_run(prompt)

    def set_branch_name(self, name: str, *, cursor: ConversationGraphCursor | None = None) -> None:
        """Rename the branch at ``cursor``'s leaf (the current cursor if omitted).

        Agent-facing API exposed via the ``update_app_state(current_branch_name=...)`` tool.
        The tool pulls the pinned cursor from ``AgentContext.conversation_cursor`` (captured
        at turn start) and passes it explicitly so a tool call mid-turn renames the *launching*
        branch — not wherever the user happens to be looking. Mirrors the cursor-pin trick the
        ``AgentStreamRouter`` uses for feed mutations.

        Renaming a node mutates ``ConversationNode.name``; branch indicators read that lazily
        on each render, but their VMs only fire ``dirty`` on selection changes — so we nudge
        the indicator on the parent's feed manually here to make the new name show up without
        waiting for an unrelated event.
        """
        target = cursor if cursor is not None else self._cursor
        node_id = target.head
        self._conversation.rename(node_id, name)

        if len(target.path) >= 2:
            parent_id = target.path[-2]
            for item in self._conversation.node(parent_id).feed:
                if isinstance(item.entry, BranchPointModel):
                    item.entry.emit(item.entry.dirty)
                    break

    def _record_visit(self, cursor: ConversationGraphCursor) -> None:
        """Record the cursor path in each node's ``last_visited_child`` cache.

        For every consecutive ``(parent, child)`` pair in the path, set the parent's
        ``last_visited_child`` to that child. Called from every cursor-mutating method so the cache
        always reflects the freshest descent below each node the cursor has ever crossed.
        """
        path = cursor.path
        for parent_id, child_id in zip(path, path[1:]):
            self._node(parent_id).last_visited_child = child_id

    def _deepen_via_last_visited(self, path: tuple[NodeId, ...]) -> tuple[NodeId, ...]:
        """Extend ``path`` by chasing ``last_visited_child`` from the tail.

        Walks down through the cache until the current tail has no recorded child or the recorded
        child is no longer a valid child of the tail (defensive — the graph is append-only so
        children don't disappear, but the cache predates a hypothetical future detach). The
        returned tuple always starts with ``path`` unchanged; only the suffix is new.

        Used by ``swap_sibling`` and ``descend_into`` so that re-entering a previously-visited
        subtree lands on the deepest previously-seen leaf rather than stopping at the swap/descent
        point.
        """
        extended = list(path)
        while True:
            tail = extended[-1]
            recorded = self._node(tail).last_visited_child
            if recorded is None:
                break
            if recorded not in self._conversation.children(tail):
                break
            extended.append(recorded)
        return tuple(extended)

    def descend_into(self, child_id: NodeId) -> None:
        """Descend the cursor into one of the leaf's children, then deepen via the
        ``last_visited_child`` cache so re-entry restores the most-recently-visited descendant
        rather than stopping at the explicitly-named child. View receives ``feed_replaced``.
        """
        new_path = self._deepen_via_last_visited(
            self._conversation.descend(self._cursor, child_id).path
        )
        self._cursor = ConversationGraphCursor(new_path)
        self._record_visit(self._cursor)
        self._sync_navigation_state()
        self.emit(self.feed_replaced)

    def ascend(self, *, parent_node_id: NodeId | None = None) -> None:
        """Truncate the cursor past a branch point.

        Default (``parent_node_id=None``) is the legacy "pop one level" semantics — useful from
        keystrokes that don't know which indicator they're under. When called from a focused
        ``BranchPointModel``, the indicator passes its ``parent_node_id`` so the cursor
        truncates to that node as its new leaf (i.e. "un-descend out of *this* branch point",
        regardless of how many levels deeper the cursor currently sits). No-op if the node isn't
        on the path or is already the leaf.

        Ascending does not chase ``last_visited_child`` — the user explicitly asked to truncate
        upward, so re-deepening would defeat the request. The cache stays warm for the next
        ``descend_into`` / ``swap_sibling``.
        """
        path = self._cursor.path
        if parent_node_id is None:
            if len(path) < 2:
                return
            new_path = path[:-1]
        else:
            try:
                i = path.index(parent_node_id)
            except ValueError:
                return
            if i + 1 >= len(path):
                return
            new_path = path[: i + 1]
        self._cursor = ConversationGraphCursor(new_path)
        self._record_visit(self._cursor)
        self._sync_navigation_state()
        self.emit(self.feed_replaced)

    def swap_sibling(self, direction: int, *, parent_node_id: NodeId | None = None) -> None:
        """Swap a horizontal sibling at a specific branch point in the cursor path.

        Default (``parent_node_id=None``) swaps the leaf's sibling — the cursor's penultimate
        node decides the swap point. When called from a focused ``BranchPointModel``, the
        indicator passes its ``parent_node_id`` so the swap happens there.

        After the swap, the cursor is deepened via ``last_visited_child`` so re-entering a
        subtree the user has visited before restores them to the previously-seen leaf (e.g. the
        cursor was at (A, B, E), user swaps to C and back, lands on (A, B, E) again). New
        siblings with no recorded descent terminate the chain at the swap point.

        No-op if the node isn't on the path, has no children in the requested direction, or is
        already the cursor's leaf (no descended child to swap from).
        """
        path = self._cursor.path
        if parent_node_id is None:
            if len(path) < 2:
                return
            parent_node_id = path[-2]
            truncate_at = len(path) - 1
        else:
            try:
                truncate_at = path.index(parent_node_id) + 1
            except ValueError:
                return
            if truncate_at >= len(path):
                return

        current_child = path[truncate_at]
        children = self._conversation.children(parent_node_id)
        try:
            idx = children.index(current_child)
        except ValueError:
            return
        new_idx = idx + direction
        if not 0 <= new_idx < len(children):
            return

        new_path = self._deepen_via_last_visited(path[:truncate_at] + (children[new_idx],))
        self._cursor = ConversationGraphCursor(new_path)
        self._record_visit(self._cursor)
        self._sync_navigation_state()
        self.emit(self.feed_replaced)

    def _sync_navigation_state(self) -> None:
        """Push cursor-derived state to sub-VMs after any cursor mutation.

        Two distinct concerns, both keyed off the current cursor:

        - **Branch indicators.** Walk the cursor path; for each node, find branch-indicator feed
          items (whose owning node is the indicator's ``parent_node_id``) and push their new
          ``selected_child`` (the path element immediately *after* that node, or ``None`` when
          the indicator's parent is the cursor's leaf). The setter is equality-guarded.
        - **Chat input + status bar.** Derive ``chat_input.enabled`` / ``chat_input.hint`` from
          the current branch's ``pending_interrupt``, and push the current branch's session
          token usage to the status bar. See :meth:`_sync_chat_input_to_cursor`.

        Called before ``feed_replaced`` is emitted so any indicator widgets the view is about to
        mount read up-to-date VM state during ``compose``.
        """
        path = self._cursor.path
        for i, nid in enumerate(path):
            for item in self._conversation.node(nid).feed:
                if isinstance(item.entry, BranchPointModel):
                    selected = path[i + 1] if i + 1 < len(path) else None
                    item.entry.set_selected_child(selected)

        self._sync_chat_input_to_cursor()

        # Status bar reflects the *current* branch's token usage. Background-branch streams that
        # fire ``on_token_usage_changed`` are filtered out in ``_on_token_usage_changed`` by
        # checking the firing session against ``self.agent_session``.
        current_session = self._node(self._cursor.head).agent_session
        if current_session is not None:
            self.status_bar.set_token_usage(current_session.token_usage)

    def _sync_chat_input_to_cursor(self) -> None:
        """Derive chat-input enabled/hint from the current branch's pending interrupt.

        Skipped in COMMIT mode — commit owns its own chat-input semantics (hint = COMMIT_HINT
        with its own enabled lifecycle), and the state machine forbids commit + interrupt
        coexistence.
        """
        if self.state != ChatPaneModel.State.CONVERSATION:
            return
        if self._node(self._cursor.head).pending_interrupt is not None:
            self.chat_input.set_enabled(False)
            self.chat_input.set_hint(_INTERRUPT_HINT)
        else:
            self.chat_input.set_enabled(True)
            self.chat_input.set_hint(_DEFAULT_HINT)

    # ------------------------------------------------------------------
    # Input dispatch
    # ------------------------------------------------------------------

    def _on_input_submitted(self, text: str) -> None:
        """Subscriber on ``chat_input.submitted``: route the submitted text to a command, a shell
        command, or an agent turn. Text is already buffer-cleared and history-pushed by the input VM
        by the time we see it.

        Agent-busy gating (no feed queue yet):
          - shell ``!`` commands always pass through
          - slash commands pass through unless the name is in ``_AGENT_GATED_COMMANDS``
          - chat text requires the agent to be idle
        Blocked submissions surface as a transient notification.
        """
        if self.state == ChatPaneModel.State.COMMIT:
            # In commit mode the input buffer is interpreted as optional commit instructions.
            self.chat_input.accept_submission(text)
            self.submit_commit_payload(text)
            return

        stripped = text.lstrip()

        if stripped.startswith("!"):
            cmd = stripped[1:].strip()
            if cmd:
                self.chat_input.accept_submission(text)
                self.start_shell_command(cmd)
            return

        if stripped.startswith("/"):

            name = stripped.lstrip("/").split(maxsplit=1)[0]
            if self.agent_busy and name in self._AGENT_GATED_COMMANDS:
                self.emit(self.notify, ChatPaneModel.NotifyAction.AGENT_BUSY)
                return

            # /branch <prompt> is intercepted here instead of going through the click registry:
            # the registry tokenizes via ``shlex_split``, which mangles quotes and crashes on
            # unbalanced apostrophes (``Can't`` → ValueError). Bare /branch (no rest after the
            # name) still falls through to the registry to hit the no-arg ``_branch`` handler.
            if name == "branch":
                rest = stripped[len("/branch"):].strip()
                if rest:
                    self.chat_input.accept_submission(text)
                    self.branch_and_send(rest)
                    return

            self.chat_input.accept_submission(text)
            self._schedule_worker(self._execute_command(stripped))
            return

        if self.agent_busy:
            self.emit(self.notify, ChatPaneModel.NotifyAction.AGENT_BUSY)
            return

        # Sitting on a non-leaf cursor (branch point) means there's no AgentSession at the current
        # leaf — it was popped when this node was branched. Plain chat text has nowhere to go;
        # prompt the user to descend into one of the branches first. ``/branch`` and other slash
        # commands aren't gated here — eventually /branch from a non-leaf will create a sibling.
        if self._conversation.children(self._cursor.head):
            self.emit(self.notify, ChatPaneModel.NotifyAction.DESCEND_REQUIRED)
            return

        self.chat_input.accept_submission(text)
        self.start_agent_run(text)

    def start_shell_command(self, cmd: str) -> None:
        """Append a shell-command VM to the feed and kick off its execute() on the worker scheduler.
        Unlike agent runs, shell commands aren't gated by ``agent_busy`` — they're side-channel to
        the conversation.
        """
        assert self.state == ChatPaneModel.State.CONVERSATION
        vm = ShellCommandModel(cmd)
        self._append_feed(vm)
        self._schedule_worker(vm.execute())


    # ------------------------------------------------------------------
    # Commit mode
    # ------------------------------------------------------------------
    #
    # State machine: CONVERSATION ↔ COMMIT. Most public API asserts CONVERSATION; the methods below
    # are the only legal way in and out of COMMIT. Selection state lives on each
    # ``AgentMessageModel`` (its own dirty drives the per-message border + checkbox); the pane
    # holds the ordered snapshot of selectable VMs and the cursor index so navigation is O(1).
    #
    # ``submit_commit_payload`` is stubbed: it cleans up decoration and returns to CONVERSATION but
    # doesn't yet build/forward the payload to the agent — that wiring (and the routing decision
    # between direct/subagent paths) lands in a follow-up.

    _COMMIT_HINT = "Type instructions for the commit (Enter to submit, may be empty)..."

    def enter_commit_mode(self) -> None:
        """Snapshot learn-mode agent messages, decorate them as the selectable set, and transition to
        COMMIT. If no learn-mode agent messages exist, append a system message and stay in
        CONVERSATION.
        """
        assert self.state == ChatPaneModel.State.CONVERSATION
        assert not self.agent_busy

        # Commit selects across the full visible conversation (including ancestor branches), not
        # just the current leaf's feed. Multi-branch commit (selecting across *all* branches per
        # the original spec) is a follow-up; for now this matches pre-branch behavior of "every
        # learn-mode message in this conversation".
        selectable = [
            item.entry for item in self.visible_feed
            if isinstance(item.entry, AgentMessageModel) and item.entry.mode == Mode.LEARN
        ]
        if not selectable:
            self.append_message(
                ChatMessageModel(role=Role.SYSTEM, content="No selectable messages to commit."),
                include_in_agent_context=False,
            )
            return

        self._commit_selectable = selectable
        self._commit_cursor = 0
        for i, vm in enumerate(selectable):
            vm.set_selectable(True)
            vm.set_cursor(i == 0)

        self.state = ChatPaneModel.State.COMMIT
        self.chat_input.set_hint(self._COMMIT_HINT)
        self.chat_input.set_state(ChatInputModel.State.COMMIT)
        # Move focus off the input so up/down/enter drive the cursor rather than the input's
        # history nav / submit. The view's focus subscription routes this to the message-area
        # scroll container; events bubble back to the pane's commit-mode bindings.
        self.request_focus()
        self.emit(self.dirty)


    def navigate_commit_cursor_up(self) -> None:
        assert self.state == ChatPaneModel.State.COMMIT
        self._move_commit_cursor(-1)


    def navigate_commit_cursor_down(self) -> None:
        assert self.state == ChatPaneModel.State.COMMIT
        self._move_commit_cursor(1)


    def _move_commit_cursor(self, delta: int) -> None:
        new_index = self._commit_cursor + delta
        if new_index < 0 or new_index >= len(self._commit_selectable):
            return
        self._commit_selectable[self._commit_cursor].set_cursor(False)
        self._commit_cursor = new_index
        self._commit_selectable[new_index].set_cursor(True)


    def toggle_include_current_message_in_commit(self) -> None:
        """Toggle the message under the cursor. On select (not deselect), auto-advance the cursor
        to the next selectable if there is one.
        """
        assert self.state == ChatPaneModel.State.COMMIT

        if not self._commit_selectable:
            return
        
        current = self._commit_selectable[self._commit_cursor]
        was_selected = current.is_selected
        current.set_selected(not was_selected)

        # Auto-advance logic
        if not was_selected and self._commit_cursor < len(self._commit_selectable) - 1:
            self._move_commit_cursor(1)


    def exit_commit_mode(self) -> None:
        """Cancel commit mode without submitting. Clears all decoration and returns to CONVERSATION."""
        assert self.state == ChatPaneModel.State.COMMIT
        self._reset_commit_state()


    def submit_commit_payload(self, instructions: str) -> None:
        """Submit the commit payload (selected messages + optional free-text instructions) and return
        to CONVERSATION.

        Builds a payload from the selected ``AgentMessageModel`` bodies (each annotated with the
        immediately-preceding USER message as ``user_context``), injects it into the agent session,
        posts a system notification with the direct-vs-subagent routing hint, and kicks off an
        agent-only turn (no USER message in the feed — the notification is the prompt).

        Edge case: if the user submitted with zero selected messages, exit COMMIT and append
        "No messages selected for commit." Mirrors the legacy ``confirm_commit_selection`` behavior.
        """
        assert self.state == ChatPaneModel.State.COMMIT

        payload = self._build_commit_payload()

        # Transition out of COMMIT first — every subsequent feed mutation / agent kick-off requires
        # CONVERSATION state, and we never re-enter COMMIT from inside this method.
        self._reset_commit_state()

        if not payload:
            self.append_message(
                ChatMessageModel(role=Role.SYSTEM, content="No messages selected for commit."),
                include_in_agent_context=False,
            )
            return

        if self.agent_session is None:
            self.append_message(
                ChatMessageModel(role=Role.ERROR, content="Agent session not bootstrapped."),
            )
            return

        self.agent_session.set_commit_payload(payload)
        self.agent_session.add_system_notification(
            self._build_commit_notification(payload, instructions),
        )
        self._start_agent_turn()

    def _build_commit_payload(self) -> list[dict]:
        """Walk ``_commit_selectable`` (in feed order) and build one dict per selected message:

            {"index": int, "content": str, "user_context": str | None}

        ``user_context`` is the most recent USER ``ChatMessageModel`` preceding this agent message
        in the feed, bailing on an earlier ``AgentMessageModel`` so we capture the immediate
        prompt rather than stale conversation. Messages with empty bodies are skipped.
        """
        payload: list[dict] = []
        for idx, vm in enumerate(self._commit_selectable):
            if not vm.is_selected or vm.is_empty:
                continue
            entry: dict = {"index": idx, "content": vm.body}
            user_context = self._preceding_user_context(vm)
            if user_context is not None:
                entry["user_context"] = user_context
            payload.append(entry)
        return payload

    def _preceding_user_context(self, vm: AgentMessageModel) -> str | None:
        """Scan backwards in the feed from ``vm``'s position for the nearest USER ChatMessageModel.
        Stop and return None if we hit an earlier AgentMessageModel first — that means the
        prompt for this segment is somewhere upstream we shouldn't conflate.

        Scans the full visible feed so that an agent message in the current leaf can correctly
        pick up the user prompt that lives in an ancestor branch's feed.
        """
        visible = self.visible_feed
        feed_pos = next((i for i, item in enumerate(visible) if item.entry is vm), None)
        if feed_pos is None:
            return None
        for item in reversed(visible[:feed_pos]):
            entry = item.entry
            if isinstance(entry, AgentMessageModel):
                return None
            if isinstance(entry, ChatMessageModel) and entry.role == Role.USER:
                return entry.content
        return None

    def _build_commit_notification(self, payload: list[dict], instructions: str) -> str:
        """Compose the system notification handed to the agent. Routing (direct vs subagent) follows
        ``Options.Subagents.Commit.*``; this matches the legacy chat pane's behavior verbatim. The
        subagent path will eventually move to the agent session itself — see the design discussion
        in this turn — but for now the pane owns the decision so we don't touch the session API.
        """
        combined = "\n".join(entry["content"] for entry in payload)
        approx_tokens = count_tokens_approximately([HumanMessage(content=combined)])
        num_messages = len(payload)

        use_subagent = False
        if self._options is not None and self._options.get(Options.Subagents.Commit.Enabled) == "enabled":
            criterion = self._options.get(Options.Subagents.Commit.RoutingCriterion)
            threshold = self._options.get(Options.Subagents.Commit.RoutingThreshold)
            if criterion == "tokens":
                use_subagent = approx_tokens >= threshold
            else:
                use_subagent = num_messages >= threshold

        if use_subagent:
            notification = (
                f"User selected {num_messages} message(s) for commit "
                f"(~{approx_tokens} tokens). Use commit_invoke_subagent to delegate "
                "knowledge entry extraction, then present the proposal to the user."
            )
        else:
            notification = (
                f"User selected {num_messages} message(s) for commit "
                f"(~{approx_tokens} tokens). Use commit_show_selected_messages and "
                "commit_proposal_create to draft entries directly, then present "
                "the proposal to the user."
            )

        if instructions:
            notification += (
                f"\n\nUser provided these additional instructions for the commit:\n{instructions}"
            )
        return notification


    def _reset_commit_state(self) -> None:
        for vm in self._commit_selectable:
            vm.clear_commit_decoration()

        self.state = ChatPaneModel.State.CONVERSATION
        self._commit_selectable = []
        self._commit_cursor = 0

        self.chat_input.set_state(ChatInputModel.State.CHAT)
        self.chat_input.reset_hint()
        self.chat_input.request_focus()
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Command registry
    # ------------------------------------------------------------------

    def _registry_rows(self) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        for name, cmd in self._command_registry.commands.items():
            desc = cmd.help or (cmd.callback.__doc__ if cmd.callback else "") or ""
            desc = desc.strip().splitlines()[0] if desc else ""
            rows.append((name, desc))
        return rows


    async def _execute_command(self, text: str) -> None:
        line = text.lstrip("/").strip()
        if not line:
            return

        try:
            result = await self._command_registry.execute(line)
        except KeyError as exc:
            self.append_message(ChatMessageModel(role=Role.ERROR, content=str(exc).strip("'")))
            return
        except Exception as exc:  # noqa: BLE001 — surface unexpected handler errors as ERROR messages
            self.append_message(ChatMessageModel(role=Role.ERROR, content=f"Command error: {exc}"))
            return

        if result is not None:
            self.append_message(
                ChatMessageModel(role=Role.SYSTEM, content=str(result), rich=True),
                include_in_agent_context=False,
            )


    def _register_commands(self) -> None:
        reg = self._command_registry

        @reg.command(name="clear", help="Clear the message feed.")
        def _clear() -> None:
            self.clear_feed()

        @reg.command(name="quit", help="Quit the application.")
        def _quit() -> None:
            self.emit(self.notify, ChatPaneModel.NotifyAction.QUIT)

        @reg.command(name="new", help="Open a new chat session tab.")
        def _new() -> None:
            self.emit(self.notify, ChatPaneModel.NotifyAction.NEW_TAB)

        @reg.command(name="close", help="Close the current chat session tab.")
        def _close() -> None:
            self.emit(self.notify, ChatPaneModel.NotifyAction.CLOSE_TAB)

        @reg.command(name="logs", help="Open the logs viewer tab.")
        def _logs() -> None:
            self.emit(self.notify, ChatPaneModel.NotifyAction.OPEN_LOGS)

        @reg.command(name="rename", help="Rename the current tab.")
        @click.argument("words", nargs=-1, required=True)
        async def _rename(words: tuple[str, ...]) -> None:
            await self.set_tab_name(" ".join(words))

        @reg.command(name="help", help="Show available commands, or details for a specific command.")
        @click.argument("command_name", default="", required=False)
        def _help(command_name: str) -> str:
            if command_name:
                name = command_name.strip().lstrip("/")
                cmd = reg.commands.get(name)
                if cmd is None:
                    return f"Unknown command: /{name}\nType /help to see available commands."
                with cmd.make_context(
                    name, [], max_content_width=reg.max_content_width, resilient_parsing=True
                ) as ctx:
                    return ctx.get_help()
            lines = ["**Available commands:**", ""]
            for cmd_name in sorted(reg.commands):
                cmd = reg.commands[cmd_name]
                desc = cmd.help or (cmd.callback.__doc__ if cmd.callback else "") or ""
                desc = desc.strip().split("\n")[0] if desc else ""
                lines.append(f"  /{cmd_name} — {desc}")
            lines.append("")
            lines.append("Commands support standard CLI syntax (options, flags, --help).")
            return "\n".join(lines)

        @reg.command(name="commit", help="Enter commit mode to select learn-mode messages.")
        def _commit() -> None:
            # TODO: port legacy ``/commit --auto [instructions]``. Needs rethinking under the
            # conversation graph — auto-commit drafts from "the conversation", but which branch's
            # feed counts as the conversation now isn't obvious.
            self.enter_commit_mode()

        @reg.command(
            name="branch",
            help="Fork a new branch. Optionally provide a prompt to send.",
        )
        def _branch() -> None:
            # The ``/branch <prompt>`` form is intercepted in ``_on_input_submitted`` so the
            # prompt never reaches click/shlex tokenization. By the time we get here, the rest
            # of the line is empty — this handler covers only the bare ``/branch`` case.
            self.branch()

        @reg.command(name="rename-branch", help="Rename the current branch.")
        @click.argument("words", nargs=-1, required=True)
        def _rename_branch(words: tuple[str, ...]) -> None:
            # No /branch-style intercept: branch names are short enough that shlex
            # tokenization is fine, and ``nargs=-1`` + ``" ".join(...)`` recovers the spacing
            # for typical names. Quotes/apostrophes would still get mangled by shlex, but
            # branch names rarely need them.
            self.set_branch_name(" ".join(words))

        @reg.command(name="idle", help="Switch to idle mode.")
        async def _idle() -> None:
            await self.set_mode(Mode.IDLE)

        @reg.command(name="learn", help="Switch to learn mode.")
        async def _learn() -> None:
            await self.set_mode(Mode.LEARN)

        @reg.command(name="review", help="Switch to review mode.")
        async def _review() -> None:
            await self.set_mode(Mode.REVIEW)

        @reg.command(name="browse", help="Open the data browser inline in the feed.")
        def _browse() -> None:
            # The browser is DB-backed end-to-end. Without a session factory
            # (test harnesses, headless invocations) we surface a system
            # message instead of mounting a non-functional widget.
            if self._session_factory is None:
                self.append_message(
                    ChatMessageModel(
                        role=Role.SYSTEM,
                        content="/browse requires a session factory; none is configured.",
                    ),
                    include_in_agent_context=False,
                )
                return
            self._append_feed(BrowserModel(self._session_factory))

        @reg.command(name="resources", help="Toggle the resource viewer side panel.")
        def _resources() -> None:
            # The panel is DB-backed (and shares the agent's ResourceManager). Without a session
            # factory there's nothing to drive it, so surface a system message instead of toggling
            # an inert panel. The view owns the actual mount/unmount — we just request the toggle.
            if self.resource_viewer is None:
                self.append_message(
                    ChatMessageModel(
                        role=Role.SYSTEM,
                        content="/resources requires a session factory; none is configured.",
                    ),
                    include_in_agent_context=False,
                )
                return
            self.emit(self.notify, ChatPaneModel.NotifyAction.TOGGLE_RESOURCE_VIEWER)

        @reg.command(name="options", help="Open the options editor inline in the feed.")
        def _options() -> None:
            # Bootstrap binds ``self._options`` to the app-level (root) Options instance. Until
            # that runs we don't have a target to edit; surface a system message instead of
            # mounting a non-functional widget.
            if self._options is None:
                self.append_message(
                    ChatMessageModel(
                        role=Role.SYSTEM,
                        content="/options is unavailable until the agent session is bootstrapped.",
                    ),
                    include_in_agent_context=False,
                )
                return
            self._append_feed(OptionsEditorModel(self._options))

        @reg.command(name="echo", help="Echo arguments back as a system message.")
        @click.argument("words", nargs=-1)
        def _echo(words: tuple[str, ...]) -> None:
            self.append_message(
                ChatMessageModel(role=Role.SYSTEM, content=" ".join(words) if words else ""),
                include_in_agent_context=False,
            )

        @reg.command(name="test-turn", help="Run a synthetic agent turn to exercise routing.")
        async def _test_turn() -> None:
            await self._run_synthetic_turn()

        @reg.command(
            name="test-flow",
            help=(
                "Stream → pause (try typing!) → interrupt → resume. "
                "Exercises mid-stream input + interrupt teardown."
            ),
        )
        async def _test_flow() -> None:
            await self._run_synthetic_flow()

        @reg.command(name="test-interrupt", help="Spawn a synthetic interrupt to exercise routing.")
        async def _test_interrupt() -> None:
            interrupt = TestInterruptModel(prompt="Pick an option:", options=["alpha", "beta", "gamma"])
            result = await self.present_interrupt(interrupt)
            if result is None:
                self.append_message(
                    ChatMessageModel(role=Role.SYSTEM, content="interrupt cancelled"),
                    include_in_agent_context=False,
                )
            else:
                self.append_message(
                    ChatMessageModel(role=Role.SYSTEM, content=f"interrupt resolved: {result!r}"),
                    include_in_agent_context=False,
                )

        @reg.command(name="test-choices", help="Spawn a Choices interrupt with sample options.")
        async def _test_choices() -> None:
            interrupt = UserChoicesModel.from_interrupt({
                "message": "Which fruit do you prefer?",
                "options": ["Apple", "Banana", "Cherry", "Durian"],
            })
            result = await self.present_interrupt(interrupt)
            content = "choices cancelled" if result is None else f"choices resolved: {result!r}"
            self.append_message(
                ChatMessageModel(role=Role.SYSTEM, content=content),
                include_in_agent_context=False,
            )

        @reg.command(name="test-warning-choices", help="Spawn a WarningChoices interrupt.")
        async def _test_warning_choices() -> None:
            interrupt = WarningUserChoicesModel.from_interrupt({
                "message": "The agent wants to delete 42 files from the working tree.",
                "options": ["Approve once", "Always approve in this session"],
            })
            result = await self.present_interrupt(interrupt)
            content = (
                "warning-choices cancelled"
                if result is None
                else f"warning-choices resolved: {result!r}"
            )
            self.append_message(
                ChatMessageModel(role=Role.SYSTEM, content=content),
                include_in_agent_context=False,
            )

        @reg.command(name="test-multiple-choices", help="Spawn a MultipleChoices interrupt with 3 questions.")
        async def _test_multiple_choices() -> None:
            interrupt = MultiUserChoicesModel.from_interrupt({
                "questions": [
                    {
                        "name": "Theme",
                        "prompt": "Which theme should the app use?",
                        "options": ["Light", "Dark", "Solarized"],
                    },
                    {
                        "name": "Editor",
                        "prompt": "Which editor binding feels right?",
                        "options": ["vim", "emacs", "default"],
                    },
                    {
                        "name": "Density",
                        "prompt": "How dense should the layout be?",
                        "options": ["compact", "comfortable", "spacious"],
                    },
                ],
            })
            result = await self.present_interrupt(interrupt)
            content = (
                "multiple-choices cancelled"
                if result is None
                else f"multiple-choices resolved: {result!r}"
            )
            self.append_message(
                ChatMessageModel(role=Role.SYSTEM, content=content),
                include_in_agent_context=False,
            )

        @reg.command(name="test-sql-confirmation", help="Spawn a SqlConfirmation interrupt with sample preview.")
        async def _test_sql_confirmation() -> None:
            interrupt = SqlConfirmationModel.from_interrupt({
                "sql": (
                    "UPDATE knowledge_entries\n"
                    "SET title = 'Renamed entry'\n"
                    "WHERE topic_id IN (SELECT id FROM topics WHERE name LIKE 'draft%');"
                ),
                "preview": {
                    "columns": ["id", "title", "topic_id"],
                    "rows": [
                        [1, "Old title one", 7],
                        [2, "Old title two", 7],
                        [3, "Something longer that should get truncated past the cell width limit", 8],
                        [4, "Old title four", 9],
                    ],
                },
                "row_count": 12,
            })
            result = await self.present_interrupt(interrupt)
            content = (
                "sql-confirmation cancelled"
                if result is None
                else f"sql-confirmation resolved: {result!r}"
            )
            self.append_message(
                ChatMessageModel(role=Role.SYSTEM, content=content),
                include_in_agent_context=False,
            )

        @reg.command(name="test-flashcards", help="Spawn a FlashcardReview interrupt with sample data.")
        async def _test_flashcards() -> None:
            from types import SimpleNamespace
            from fsrs import Card

            def _starter_card(card_id: int) -> Card:
                # Default Card() lands in State.Learning, step 0, due=now — exactly what we want
                # for manual exercise of the FSRS step ladder.
                c = Card()
                c.card_id = card_id
                return c

            sample_cards = [
                {"id": 101, "question": "What is the time complexity of binary search?",
                 "answer": "O(log n) — each comparison halves the remaining search space.",
                 "fsrs_card": _starter_card(101)},
                {"id": 102, "question": "Explain the difference between a stack and a queue.",
                 "answer": "A stack is LIFO: the most recently added element is removed first.\n\n"
                           "A queue is FIFO: the earliest added element is removed first.",
                 "fsrs_card": _starter_card(102)},
                {"id": 103, "question": "What is a hash collision and how is it typically resolved?",
                 "answer": "A hash collision occurs when two different keys produce the same hash value.\n\n"
                           "Common resolution strategies:\n"
                           "• Chaining — each bucket holds a linked list of entries\n"
                           "• Open addressing — probe for the next available slot",
                 "fsrs_card": _starter_card(103)},
                {"id": 204, "question": "What does the CAP theorem state?",
                 "answer": "A distributed system can provide at most two of:\n\n"
                           "• Consistency — every read returns the most recent write\n"
                           "• Availability — every request receives a response\n"
                           "• Partition tolerance — operates despite network partitions",
                 "fsrs_card": _starter_card(204)},
                {"id": 205, "question": "What is the difference between concurrency and parallelism?",
                 "answer": "Concurrency is about dealing with multiple tasks at once (structure).\n"
                           "Parallelism is about doing multiple tasks at once (execution).\n\n"
                           "Concurrency is possible on a single core via interleaving; parallelism "
                           "requires multiple cores.",
                 "fsrs_card": _starter_card(205)},
            ]

            # Inert session factory — the VM holds it only for the optional commit() API, which the
            # test command never invokes.
            class _FakeSession:
                async def __aenter__(self): return self
                async def __aexit__(self, *_): return False
                async def commit(self, *_): return
            def _fake_session_factory(): return _FakeSession()

            # Fake scorer — tweak the mapping to exercise different auto-score outcomes. ID 205 is
            # intentionally omitted to exercise the failure-fallback path.
            auto_score_results = {101: 3, 102: 1, 103: 2, 204: 4}

            class _FakeScorer:
                def __init__(self, results_by_id: dict[int, int]):
                    self._results_by_id = results_by_id
                    self.structured_response = None
                async def ainvoke(self, prompt: str):
                    await asyncio.sleep(1.5)
                    results = [
                        SimpleNamespace(flashcard_id=i, score=s, feedback="")
                        for i, s in self._results_by_id.items()
                    ]
                    self.structured_response = SimpleNamespace(results=results)

            interrupt = FlashcardReviewInterruptModel(
                cards=sample_cards,
                session_factory=_fake_session_factory,
                auto_score_enabled=True,
                auto_scorer=_FakeScorer(auto_score_results),
            )
            result = await self.present_interrupt(interrupt)
            content = (
                "flashcards cancelled" if result is None
                else f"flashcards resolved: completed={result['completed']}, {len(result['cards'])} cards"
            )
            self.append_message(
                ChatMessageModel(role=Role.SYSTEM, content=content),
                include_in_agent_context=False,
            )

        @reg.command(name="test-commit-proposal", help="Spawn a CommitProposal interrupt with sample data.")
        @click.option("--big", is_flag=True, help="Spawn 10× the sample entries to exercise sizing/scroll.")
        async def _test_commit_proposal(big: bool) -> None:
            sample_entries = [
                Entry(
                    title="Binary search complexity",
                    content="Binary search has O(log n) time complexity — each comparison halves the "
                    "remaining search space.",
                    entry_type=EntryType.FACT,
                    topic_id=1,
                    topic_name="Algorithms",
                ),
                Entry(
                    title="Stack vs queue",
                    content="A stack is LIFO: the most recently added element is removed first.\n"
                    "A queue is FIFO: the earliest added element is removed first.",
                    entry_type=EntryType.EXPOSITION,
                    topic_id=1,
                    topic_name="Algorithms",
                ),
                Entry(
                    title="Hash collisions",
                    content="A hash collision is when two distinct keys produce the same hash. Common "
                    "resolutions: chaining (buckets hold linked lists) or open addressing (probe for "
                    "the next free slot).",
                    entry_type=EntryType.FACT,
                    topic_id=None,
                    topic_name=None,
                ),
                Entry(
                    title="CAP theorem",
                    content="A distributed system can provide at most two of: Consistency, "
                    "Availability, Partition tolerance.",
                    entry_type=EntryType.OVERVIEW,
                    topic_id=2,
                    topic_name="Distributed systems",
                ),
            ]

            if big:
                sample_entries = [e.clone() for e in sample_entries for _ in range(10)]

            interrupt = CommitProposalInterruptModel(sample_entries, session_factory=self._session_factory)
            result = await self.present_interrupt(interrupt)
            if result is None or result["accepted"] is None:
                content = "commit-proposal cancelled"
            else:
                accepted = result["accepted"]
                ei = result["edit_instructions"]
                content = (
                    f"commit-proposal resolved: {len(accepted)} accepted"
                    + (f" · edits: {ei!r}" if ei else "")
                )
            self.append_message(
                ChatMessageModel(role=Role.SYSTEM, content=content),
                include_in_agent_context=False,
            )

        @reg.command(name="test-flashcard-proposal", help="Spawn a FlashcardProposal interrupt with sample data.")
        @click.option("--big", is_flag=True, help="Spawn 10× the sample flashcards to exercise sizing/scroll.")
        async def _test_flashcard_proposal(big: bool) -> None:
            sample_flashcards = [
                Flashcard(
                    question="What is the time complexity of binary search?",
                    answer="O(log n) — each comparison halves the remaining search space.",
                    testing_notes="Accept any equivalent phrasing (logarithmic, log base 2, etc.).",
                    topic_id=1,
                    topic_name="Algorithms",
                    entry_ids=[101, 102],
                ),
                Flashcard(
                    question="Explain the difference between a stack and a queue.",
                    answer="A stack is LIFO: the most recently added element is removed first.\n"
                    "A queue is FIFO: the earliest added element is removed first.",
                    testing_notes="Both LIFO/FIFO labels must be stated; pure 'opposite' answers fail.",
                    topic_id=1,
                    topic_name="Algorithms",
                    entry_ids=[103],
                ),
                Flashcard(
                    question="What is a hash collision and how is it typically resolved?",
                    answer="A collision is two distinct keys hashing to the same bucket. Common "
                    "resolutions: chaining (linked lists per bucket) or open addressing (probe "
                    "for the next free slot).",
                    testing_notes="",
                    topic_id=None,
                    topic_name=None,
                    entry_ids=[],
                ),
                Flashcard(
                    question="What does the CAP theorem state?",
                    answer="A distributed system can provide at most two of: Consistency, "
                    "Availability, Partition tolerance.",
                    testing_notes="All three properties must be named.",
                    topic_id=2,
                    topic_name="Distributed systems",
                    entry_ids=[204, 205, 206],
                ),
            ]

            if big:
                sample_flashcards = [f.clone() for f in sample_flashcards for _ in range(10)]

            interrupt = FlashcardProposalInterruptModel(sample_flashcards, session_factory=self._session_factory)
            result = await self.present_interrupt(interrupt)
            if result is None or result["accepted"] is None:
                content = "flashcard-proposal cancelled"
            else:
                accepted = result["accepted"]
                ei = result["edit_instructions"]
                content = (
                    f"flashcard-proposal resolved: {len(accepted)} accepted"
                    + (f" · edits: {ei!r}" if ei else "")
                )
            self.append_message(
                ChatMessageModel(role=Role.SYSTEM, content=content),
                include_in_agent_context=False,
            )

    async def _run_synthetic_turn(self) -> None:
        """Drive the router without invoking the real agent.

        Mounts the thinking indicator via the router's constructor, streams some markdown, emits a
        synthetic tool call, streams more, then closes. Useful as an eyeball test of the
        per-segment routing + delta-streaming.
        """
        pinned_node = self._node(self._cursor.head)
        router = AgentStreamRouter(self)
        pinned_node.current_router = router

        try:
            await asyncio.sleep(2)
            for chunk in ("Sure, let me ", "think about ", "**that** for ", "a moment.\n\n"):
                router.route_chunk(chunk)
                await asyncio.sleep(0.08)

            router.route_tool_call("search_entries", {"query": "mvvm refactor", "limit": 10})
            await asyncio.sleep(0.3)

            router.route_tool_call("list_topics", {})
            await asyncio.sleep(0.3)

            for chunk in ("Here's what I found:\n\n", "- Item one\n", "- Item two\n", "- Item three\n"):
                router.route_chunk(chunk)
                await asyncio.sleep(0.08)

        finally:
            router.close()
            if pinned_node.current_router is router:
                pinned_node.current_router = None

    async def _run_synthetic_flow(self) -> None:
        """End-to-end flow exerciser: streams, pauses long enough for the user to submit something
        mid-stream, emits an interrupt, then resumes in a fresh agent segment. Useful for eyeballing
        the reference-only routing — anything submitted during the pause should land between the
        two segments, while the first stays "open" and keeps receiving chunks.
        """
        pinned_node = self._node(self._cursor.head)
        router = AgentStreamRouter(self)
        pinned_node.current_router = router

        try:
            for chunk in ("Starting a longer turn — ", "I'll pause in a moment ", "so you can chime in.\n\n"):
                router.route_chunk(chunk)
                await asyncio.sleep(0.08)

            router.route_tool_call("search_entries", {"query": "mid-stream", "limit": 5})
            await asyncio.sleep(0.3)

            router.route_chunk("(pausing ~6s — try `/echo hello` or `/learn` now)\n\n")
            await asyncio.sleep(6.0)

            for chunk in ("Back. ", "Now I need to ask you something.\n\n"):
                router.route_chunk(chunk)
                await asyncio.sleep(0.08)

            interrupt = TestInterruptModel(
                prompt="Continue with which branch?", options=["left", "right", "neither"],
            )
            result = await self.present_interrupt(interrupt)

            if result is None:
                router.route_chunk("Interrupt cancelled — wrapping up.\n")
            else:
                router.route_chunk(f"Got it: **{result}**. Continuing.\n\n")
                for chunk in ("- step one\n", "- step two\n", "- done\n"):
                    router.route_chunk(chunk)
                    await asyncio.sleep(0.08)

        finally:
            router.close()
            if pinned_node.current_router is router:
                pinned_node.current_router = None