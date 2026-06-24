"""Chat input — sub-VM + view used by the MVVM chat area.

The input owns the visible buffer, the coarse input ``State`` (which drives enabled-ness and the
placeholder hint), and the per-session history ring. It holds a reference to the shared
``CommandPaletteModel`` (constructed by the chat area) so that buffer mutations can update palette filtering and
so that Enter-on-visible-palette can ask the palette directly whether the typed text is a complete
command — no widget-tree walks, no parent mediation.

The chat area subscribes to the input's ``Callbacks.OnSubmitted`` group to learn when to dispatch text (chat vs
slash vs agent-busy gating all stays on the chat area). Owners drive ``set_state`` to gate the surface — e.g.
disabling it while a branch interrupt is pending.
"""

from __future__ import annotations

import time
from enum import Enum

from textual.events import Blur, Focus
from textual.widgets import TextArea

from rhizome.app.model import ViewModelBase
from rhizome.app.chat_area.command_palette import CommandPaletteModel


class ChatInputModel(ViewModelBase):

    class Callbacks(ViewModelBase.Callbacks):
        OnSubmitted = "OnSubmitted"

    class State(Enum):
        """Coarse input-VM state, set by owners via ``set_state``. A single value drives three things —
        submit/history behavior, whether the surface is enabled (see the ``enabled`` property), and the
        placeholder hint (mapped view-side).

        - ``CHAT``: default, enabled. ``submit`` no-ops on empty buffers; up/down navigate command history.
        - ``COMMIT``: enabled. ``submit`` fires on empty buffers (so Enter submits the commit with no
          additional instructions); up/down skip history nav and fall through to the underlying TextArea
          cursor movement instead.
        - ``DISABLED``: disabled — a generic "input unavailable" state.
        - ``DISABLED_PENDING_INTERRUPT``: disabled because a prompt on the current branch must be resolved
          first.

        The input doesn't know *why* it's in a given state — it just follows the rules.
        """
        CHAT = "chat"
        COMMIT = "commit"
        DISABLED = "disabled"
        DISABLED_PENDING_INTERRUPT = "disabled_pending_interrupt"

    def __init__(self, palette: CommandPaletteModel) -> None:
        super().__init__()

        self.make_callback_groups({self.Callbacks.OnSubmitted: str})

        self._palette = palette

        self.state: ChatInputModel.State = ChatInputModel.State.CHAT
        self.buffer: str = ""

        # Per-session history ring. ``index == -1`` means "not currently navigating history" (the live buffer
        # is the user's working draft). On entry into history, the live buffer is preserved in ``_draft`` so
        # down-arrow can restore it on exit.
        self._history: list[str] = []
        self._history_index: int = -1
        self._draft: str = ""

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def palette(self) -> CommandPaletteModel:
        return self._palette

    @property
    def enabled(self) -> bool:
        """Inferred from state: the two ``DISABLED_*`` states gray out the surface; ``CHAT``/``COMMIT``
        are live."""
        return self.state not in (self.State.DISABLED, self.State.DISABLED_PENDING_INTERRUPT)

    @property
    def shell_mode(self) -> bool:
        """True when the buffer parses as a single-line ``!``-prefixed shell command. The view reads this to
        swap in the shell-mode border color."""
        return self.buffer.startswith("!") and "\n" not in self.buffer

    # ------------------------------------------------------------------
    # Buffer / state mutators
    # ------------------------------------------------------------------

    def set_buffer(self, text: str, *, update_palette: bool = True) -> None:
        if self.buffer == text:
            return
        self.buffer = text

        # In COMMIT state the buffer is free-text commit instructions, not slash-command input —
        # feeding it to the palette would surface a misleading "/c…" match on the first character.
        # ``update_palette=False`` is used by history nav: recalling a "/foo" entry should not pop
        # the palette open, since that would steal up/down from further history traversal.
        if update_palette and self.state == ChatInputModel.State.CHAT:
            self._palette.update_for_input(text)
        self.emit(self.Callbacks.OnDirty)

    def set_state(self, state: "ChatInputModel.State") -> None:
        if self.state == state:
            return
        self.state = state

        # Reconcile the palette with the new state: only CHAT re-filters against the live buffer. Every
        # other state hides it — COMMIT free-text and a disabled surface both have no business surfacing
        # slash-command matches.
        if state == ChatInputModel.State.CHAT:
            self._palette.update_for_input(self.buffer)
        else:
            self._palette.update_for_input("")
        self.emit(self.Callbacks.OnDirty)

    # ------------------------------------------------------------------
    # Submission
    # ------------------------------------------------------------------

    def submit(self) -> None:
        """Fire OnSubmitted with the current buffer (stripped). In CHAT state, no-op on empty
        buffers; in COMMIT state, empty submissions are allowed (Enter submits the commit with no
        additional instructions). Does NOT clear the buffer or push history — subscribers decide
        whether the submission is accepted (e.g. the chat area gates some commands while the agent is
        busy) and call ``accept_submission(text)`` to commit the clear+history-push only on accept.
        """
        text = self.buffer.strip()
        if not text and self.state != ChatInputModel.State.COMMIT:
            return
        self.emit(self.Callbacks.OnSubmitted, text)

    def accept_submission(self, text: str) -> None:
        """Commit a submission: clear the buffer and record ``text`` in history. Called by subscribers after
        they accept the submitted text — rejected/gated submissions skip this so the user can edit and retry.
        """
        self._push_history(text)
        self.set_buffer("")

    def _push_history(self, text: str) -> None:
        self._history.append(text)
        self._history_index = -1
        self._draft = ""

    # ------------------------------------------------------------------
    # History navigation
    # ------------------------------------------------------------------

    def can_history_prev(self) -> bool:
        if not self._history:
            return False
        if self._history_index == -1:
            return True
        return self._history_index > 0

    def history_prev(self) -> None:
        if not self.can_history_prev():
            return
        if self._history_index == -1:
            self._draft = self.buffer
            self._history_index = len(self._history) - 1
        else:
            self._history_index -= 1
        self.set_buffer(self._history[self._history_index], update_palette=False)

    def can_history_next(self) -> bool:
        return self._history_index >= 0

    def history_next(self) -> None:
        if not self.can_history_next():
            return
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self.set_buffer(self._history[self._history_index], update_palette=False)
        else:
            self._history_index = -1
            restored = self._draft
            self._draft = ""
            self.set_buffer(restored, update_palette=False)

    # ------------------------------------------------------------------
    # Palette pass-throughs (so the view never touches the palette directly)
    # ------------------------------------------------------------------

    def move_palette_cursor(self, delta: int) -> None:
        self._palette.move_cursor(delta)

    def confirm_palette_selection(self) -> None:
        """Tab-completion: replace the buffer with ``/<selected> ``."""
        name = self._palette.selected_command
        if name is None:
            return
        self.set_buffer(f"/{name} ")

    def palette_has_exact_match(self) -> bool:
        return self._palette.has_exact_match(self.buffer)
