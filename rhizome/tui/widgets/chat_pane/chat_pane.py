"""ChatPane view — the chat pane's orchestrator.

Composes the conversation (``ConversationArea``, center) and docks the side-panel resource viewer
on demand. It owns nothing about the conversation itself; its job is layout + the workspace-level
actions the conversation escalates to it.

Two escalation channels arrive as Textual messages from ``ConversationArea`` (forwarded from the
conversation VM's notify channels):

  - ``WorkspaceAction(action)`` — open/close tabs, quit, open logs, toggle the resource viewer.
  - ``TabRenamed(name)`` — relabel the enclosing ``ChatTabPane``.

This is the seam where future regions (graph visualizer, file reader, more side panels) get
composed alongside the conversation.
"""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.css.query import NoMatches

from rhizome.tui.widgets.view_base import ViewBase
from rhizome.tui.keybindings import Keybind
from rhizome.tui.widgets.resource_viewer import ResourceViewer
from rhizome.tui.widgets.chat_pane.chat_input import ChatInput
from rhizome.tui.widgets.chat_pane.conversation_area import ConversationArea
from rhizome.app.chat_pane.chat_pane import ChatPaneModel
from rhizome.app.chat_pane.conversation_area import ConversationAreaModel
from rhizome.app.chat_pane.messages.static import ChatMessageModel
from rhizome.tui.types import ChatMessageData


class ChatPane(ViewBase[ChatPaneModel]):

    BINDINGS = [
        # Resource viewer: alt+r focuses (opening it first if closed), alt+w closes. Non-priority,
        # so they bubble up here from the conversation. (ctrl+shift+* is avoided — terminals can't
        # distinguish it from ctrl+*.)
        Keybind.ChatFocusResourceViewer.as_binding("focus_resource_viewer", show=False),
        Keybind.ChatCloseResourceViewer.as_binding("close_resource_viewer", show=False),
    ]

    DEFAULT_CSS = """
    ChatPane {
        layout: vertical;
        height: 1fr;
    }
    /* Left-docked resource viewer panel. ``dock`` lifts it out of the vertical flow and pins it to
       the left edge; the conversation area fills the remainder. Fixed width here overrides the
       panel's own ``width: 1fr`` (id selector wins on specificity). */
    ChatPane #resource-viewer-panel {
        dock: left;
        width: 56;
        height: 1fr;
        background: $surface-darken-1;
    }
    ChatPane #conversation-area {
        width: 1fr;
        height: 1fr;
    }
    """

    def __init__(self, *, services, show_welcome: bool = False, **kwargs) -> None:
        super().__init__(ChatPaneModel(services, show_welcome=show_welcome), **kwargs)

    def compose(self) -> ComposeResult:
        yield ConversationArea(self._vm.conversation_area, id="conversation-area")

    # ------------------------------------------------------------------
    # Escalations from the conversation
    # ------------------------------------------------------------------

    @on(ConversationArea.WorkspaceAction)
    def _on_workspace_action(self, event: ConversationArea.WorkspaceAction) -> None:
        handler = self._WORKSPACE_HANDLERS.get(event.action)
        if handler is not None:
            handler(self)

    @on(ConversationArea.TabRenamed)
    def _on_tab_renamed(self, event: ConversationArea.TabRenamed) -> None:
        pane = self._enclosing_tab_pane()
        if pane is None:
            return
        pane.full_name = event.name
        pane._update_tab_label()

    # ------------------------------------------------------------------
    # Workspace action handlers
    # ------------------------------------------------------------------
    #
    # Tab/app actions (quit, new/close tab, open logs) are global slash commands now — their handlers
    # live on the App, which dispatch reaches through the command registry. What stays here is the
    # resource-viewer toggle: a per-pane widget mount/unmount the VM can't perform itself.

    def _toggle_resource_viewer(self) -> None:
        # Toggle the left-docked panel. The VM persists on this orchestrator VM, so closing and
        # reopening preserves the panel's load/link/cursor state. Open → focus the panel
        # (auto-delegates inward); close → return focus to the conversation input.
        try:
            panel = self.query_one("#resource-viewer-panel", ResourceViewer)
        except NoMatches:
            panel = None
        if panel is not None:
            panel.remove()
            self.query_one("#chat-input", ChatInput).focus()
            return
        vm = self._vm.resource_viewer
        if vm is None:
            return
        panel = ResourceViewer(vm, id="resource-viewer-panel")
        self.mount(panel)
        panel.focus()

    _WORKSPACE_HANDLERS = {
        ConversationAreaModel.NotifyAction.TOGGLE_RESOURCE_VIEWER: _toggle_resource_viewer,
    }

    # ------------------------------------------------------------------
    # Resource viewer actions (keybinds)
    # ------------------------------------------------------------------

    def action_focus_resource_viewer(self) -> None:
        # alt+r from anywhere in the pane: ensure the panel is open, then focus its tree. Reuses the
        # persisted VM so a reopen restores prior load/link/cursor state.
        try:
            panel = self.query_one("#resource-viewer-panel", ResourceViewer)
        except NoMatches:
            vm = self._vm.resource_viewer
            if vm is None:
                return
            panel = ResourceViewer(vm, id="resource-viewer-panel")
            self.mount(panel)
        # Deferred so it works for a just-mounted panel (the tree isn't composed until after refresh).
        panel.call_after_refresh(lambda: self._focus_panel_tree(panel))

    @staticmethod
    def _focus_panel_tree(panel: ResourceViewer) -> None:
        try:
            panel.query_one("#rv-loader-tree").focus()
        except NoMatches:
            pass

    def action_close_resource_viewer(self) -> None:
        # alt+w: close the panel and return focus to the conversation input. No-op when closed.
        try:
            panel = self.query_one("#resource-viewer-panel", ResourceViewer)
        except NoMatches:
            return
        panel.remove()
        self.query_one("#chat-input", ChatInput).focus()

    # ------------------------------------------------------------------
    # Tab plumbing
    # ------------------------------------------------------------------

    def _enclosing_tab_pane(self):
        from rhizome.tui.screens.main import ChatTabPane
        node = self.parent
        while node is not None:
            if isinstance(node, ChatTabPane):
                return node
            node = node.parent
        return None

    # ------------------------------------------------------------------
    # Compatibility shim — MainScreen builds the legacy ``ChatMessageData`` for whichever pane is
    # active; the MVVM feed speaks ``ChatMessageModel``, so convert at this boundary. Folds away as
    # those call sites migrate to VM-native APIs.
    # ------------------------------------------------------------------

    def append_message(self, msg: ChatMessageData) -> None:
        self._vm.append_message(
            ChatMessageModel(role=msg.role, content=msg.content, mode=msg.mode, rich=msg.rich)
        )
