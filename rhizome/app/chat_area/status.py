"""StatusBarModel — a read-only projection of the conversation's live agent settings.

The status bar shows facts that are owned elsewhere; this VM holds none of them, it only subscribes and
re-emits a single ``OnDirty`` so the view repaints:

- **mode + verbosity** — per-branch, on the checked-out node's ``AppContextStore`` (the SSOT). Because
  they are per-branch, the chat area re-points this VM at the current leaf's store on every cursor move
  via ``set_app_state``; the VM swaps its subscription and re-reads.
- **model name** — conversation-global, on the ``OptionService`` (``Options.Agent.Model``). Subscribed
  once at construction. ``Model``'s effective value is conditional on ``Provider``, so a re-read is wired
  to both specs rather than trusting a single change to cover the conditional default.

Both sources hold subscribers weakly (the ``CallbackHost`` contract), so the bound-method handlers below
survive only while this VM is held strongly — which it is, by the ``ChatAreaModel`` that owns it.
"""

from __future__ import annotations

from rhizome.agent.app_context import AppContextStore
from rhizome.app.model import ViewModelBase
from rhizome.app.options import OptionService, Options


class StatusBarModel(ViewModelBase):

    def __init__(
        self,
        options: OptionService | None = None,
        app_state: AppContextStore | None = None,
    ) -> None:
        super().__init__()

        self._options = options
        self._app_state: AppContextStore | None = None

        self.mode: str = "idle"
        self.verbosity: str = "auto"
        self.model_name: str = options.get(Options.Agent.Model) if options is not None else ""

        # Model name reacts to Provider/Model edits. Subscribed once — the options service is
        # conversation-global, unlike the per-branch app_state below.
        if options is not None:
            options.subscribe_on_changed(Options.Agent.Provider, self._on_model_option_changed)
            options.subscribe_on_changed(Options.Agent.Model, self._on_model_option_changed)

        if app_state is not None:
            self.set_app_state(app_state)

    # ------------------------------------------------------------------
    # Per-branch source: mode + verbosity (the current leaf's AppContextStore)
    # ------------------------------------------------------------------

    def set_app_state(self, app_state: AppContextStore) -> None:
        """Point the bar at a node's live settings store, swapping the subscription off any previous one.
        The chat area calls this on every cursor move so the bar tracks the checked-out branch."""
        if app_state is self._app_state:
            return
        if self._app_state is not None:
            self._app_state.unsubscribe(AppContextStore.Callbacks.OnModeChanged, self._on_mode_changed)
            self._app_state.unsubscribe(
                AppContextStore.Callbacks.OnVerbosityChanged, self._on_verbosity_changed
            )

        self._app_state = app_state
        app_state.subscribe(AppContextStore.Callbacks.OnModeChanged, self._on_mode_changed)
        app_state.subscribe(AppContextStore.Callbacks.OnVerbosityChanged, self._on_verbosity_changed)

        self.mode = app_state.mode
        self.verbosity = app_state.verbosity
        self.emit(self.Callbacks.OnDirty)

    def _on_mode_changed(self, _old: str, new: str) -> None:
        self.mode = new
        self.emit(self.Callbacks.OnDirty)

    def _on_verbosity_changed(self, _old: str, new: str) -> None:
        self.verbosity = new
        self.emit(self.Callbacks.OnDirty)

    # ------------------------------------------------------------------
    # Conversation-global source: model name (the OptionService)
    # ------------------------------------------------------------------

    def _on_model_option_changed(self, _old, _new) -> None:
        if self._options is None:
            return
        model = self._options.get(Options.Agent.Model)
        if model == self.model_name:
            return
        self.model_name = model
        self.emit(self.Callbacks.OnDirty)
