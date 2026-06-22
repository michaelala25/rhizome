# rhizome/tui/screens/

Textual `Screen` subclasses — each file corresponds to a major UI state.

## Files

- **main.py** — `MainScreen`: wraps a `TabbedContent` containing one or more `TabPane`s. Defines `ChatTabPane` (wraps a `Workspace` for chat sessions) and `LogTabPane` (wraps `LoggingPane` for log viewing). Manages tab lifecycle (`_add_tab`, `_add_log_tab`, `_close_active_tab`). Provides `active_pane` property (returns any `TabPane`) and `post_feedback(text, severity)` which posts a `UserFeedback` message to the active pane — displayed as a toast. Reaches the conversation through `pane.workspace` (e.g. to focus the chat input on tab switch / `focus_chat`). Keybindings: `Ctrl+N` (new tab), `Ctrl+W` (close tab), `Ctrl+PageUp`/`Ctrl+PageDown` (switch tabs). The conversation's own bindings live on the `Workspace`/`ChatArea` (today: `Ctrl+C` cancel); `Ctrl+G` (open logs in editor) lives on `LoggingPane`.

- **topic_selector.py** — `TopicSelectorScreen(ModalScreen)`: lightweight modal for picking a topic from the tree. Reuses `TopicTree` from `topic_tree.py` for lazy-loading topic navigation. Dismisses with `(topic_id, topic_name)` on selection or `None` on Escape.

Future screens (not yet implemented): context selection, review, options.
