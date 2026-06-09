"""Status bar — sub-VM + view used by the MVVM chat pane.

The status bar is a projection of facts that live elsewhere: mode on the pane VM, token_usage +
model_name on the AgentSession, verbosity on app.options. Rather than have the view reach into all
three, ``StatusBarModel`` owns the projected slice. Each source's update path
writes through to a setter here; the setter no-ops on no change and emits ``dirty`` otherwise —
giving the bar repaint isolation from the rest of the pane's dirty churn (token usage in particular
updates on every model chunk).

The view ports the legacy ``widgets/status_bar.py`` render verbatim, sourced from the VM instead of
Textual reactives.
"""

from __future__ import annotations


from rhizome.agent.utils import TokenUsageData

from rhizome.app.model import ViewModelBase


class StatusBarModel(ViewModelBase):

    def __init__(self) -> None:
        super().__init__()
        self.mode: str = "idle"
        self.model_name: str = ""
        self.verbosity: str = "auto"
        self.token_usage: TokenUsageData = TokenUsageData()

    def set_mode(self, mode: str) -> None:
        if self.mode == mode:
            return
        self.mode = mode
        self.emit(self.Callbacks.OnDirty)

    def set_model_name(self, name: str) -> None:
        if self.model_name == name:
            return
        self.model_name = name
        self.emit(self.Callbacks.OnDirty)

    def set_verbosity(self, verbosity: str) -> None:
        if self.verbosity == verbosity:
            return
        self.verbosity = verbosity
        self.emit(self.Callbacks.OnDirty)

    def set_token_usage(self, usage: TokenUsageData) -> None:
        """Token usage is mutated in-place on the AgentSession, so identity checks won't catch
        updates. Always emit — callers (the agent session callback) only fire on actual updates
        anyway."""
        self.token_usage = usage
        self.emit(self.Callbacks.OnDirty)
