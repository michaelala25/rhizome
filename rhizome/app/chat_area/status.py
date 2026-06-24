"""StatusBarModel — a read-only projection of the conversation's live agent settings.

The status bar shows facts that are owned elsewhere; this VM stores none of them. Every displayed value
is a lazy ``@property`` that peeks at its SSOT on read, and every subscription routes to a single
``OnDirty`` so the view repaints. The two sources:

- **mode + verbosity** — per-branch, on the checked-out node's ``LocalAppContextStore``. Because they are
  per-branch, the chat area re-points this VM at the current leaf's store on every cursor move via
  ``set_app_state``, which swaps the subscription onto the new store.
- **model name + provider-specific knobs** — conversation-global, on the ``OptionService``. The provider
  (``Options.Agent.Provider``) selects both the effective model and which provider knobs apply; the
  Anthropic ones (adaptive thinking, effort) read as ``None`` under any other provider so the view drops
  them.

Both sources hold subscribers weakly (the ``CallbackHost`` contract), so the bound-method handler below
survives only while this VM is held strongly — which it is, by the ``ChatAreaModel`` that owns it.

The lone exception is ``usage_report``: it has no peekable SSOT, it is *pushed* in by the stream router,
so it stays stored state rather than a property.
"""

from __future__ import annotations

from rhizome.agent.app_context import LocalAppContextStore
from rhizome.agent.engine import UsageReport
from rhizome.app.model import ViewModelBase
from rhizome.app.options import OptionService, Options


class StatusBarModel(ViewModelBase):

    def __init__(
        self,
        options: OptionService | None = None,
        app_state: LocalAppContextStore | None = None,
    ) -> None:
        super().__init__()

        self._options = options
        self._app_state: LocalAppContextStore | None = None

        # Latest usage report for the checked-out branch. Pushed live by the stream router during a run on
        # the visible branch, and re-read from the leaf node's cache on every cursor move. The view paints
        # it on line 3 of the bar (context fill + per-category breakdown + cache split).
        self.usage_report: UsageReport | None = None

        # The model name and the provider-specific knobs all react to option edits; every spec routes to
        # the same repaint. The options service is conversation-global, unlike the per-branch app_state.
        if options is not None:
            for spec in (
                Options.Agent.Provider,
                Options.Agent.Model,
                Options.Agent.Anthropic.AdaptiveThinking,
                Options.Agent.Anthropic.Effort,
                Options.Agent.Anthropic.PromptCache,
                Options.Agent.Anthropic.PromptCacheTTL,
            ):
                options.subscribe_on_changed(spec, self._on_dirty)

        if app_state is not None:
            self.set_app_state(app_state)

    def _on_dirty(self, *_) -> None:
        self.emit(self.Callbacks.OnDirty)

    # ------------------------------------------------------------------
    # Per-branch source: mode + verbosity (the current leaf's LocalAppContextStore)
    # ------------------------------------------------------------------

    @property
    def mode(self) -> str:
        return self._app_state.mode if self._app_state is not None else "idle"

    @property
    def verbosity(self) -> str:
        return self._app_state.verbosity if self._app_state is not None else "auto"

    def set_app_state(self, app_state: LocalAppContextStore) -> None:
        """Point the bar at a node's live settings store, swapping the subscription off any previous one.
        The chat area calls this on every cursor move so the bar tracks the checked-out branch."""
        if app_state is self._app_state:
            return
        if self._app_state is not None:
            self._app_state.unsubscribe(LocalAppContextStore.Callbacks.OnModeChanged, self._on_dirty)
            self._app_state.unsubscribe(LocalAppContextStore.Callbacks.OnVerbosityChanged, self._on_dirty)

        self._app_state = app_state
        app_state.subscribe(LocalAppContextStore.Callbacks.OnModeChanged, self._on_dirty)
        app_state.subscribe(LocalAppContextStore.Callbacks.OnVerbosityChanged, self._on_dirty)
        self.emit(self.Callbacks.OnDirty)

    def set_usage_report(self, report: UsageReport | None) -> None:
        """Store the checked-out branch's usage report and repaint. Called by the stream router as a run
        progresses on the visible branch, and by the chat area on every cursor move."""
        self.usage_report = report
        self.emit(self.Callbacks.OnDirty)

    # ------------------------------------------------------------------
    # Conversation-global source: model + provider knobs (the OptionService)
    # ------------------------------------------------------------------

    @property
    def model_name(self) -> str:
        return self._options.get(Options.Agent.Model) if self._options is not None else ""

    @property
    def adaptive_thinking(self) -> bool | None:
        """Whether Anthropic adaptive thinking is on, or ``None`` when the active provider isn't Anthropic
        (the view drops the segment entirely in that case)."""
        if self._options is None or self._options.get(Options.Agent.Provider) != "anthropic":
            return None
        return self._options.get(Options.Agent.Anthropic.AdaptiveThinking) == "enabled"

    @property
    def effort(self) -> str | None:
        """Anthropic reasoning effort level, or ``None`` under any other provider."""
        if self._options is None or self._options.get(Options.Agent.Provider) != "anthropic":
            return None
        return self._options.get(Options.Agent.Anthropic.Effort)

    @property
    def prompt_cache_ttl(self) -> str | None:
        """The Anthropic prompt-cache breakpoint TTL ('5m'/'1h'/'dynamic'), or ``None`` under any other
        provider or while caching is off (in which case the view drops the segment)."""
        if self._options is None or self._options.get(Options.Agent.Provider) != "anthropic":
            return None
        if self._options.get(Options.Agent.Anthropic.PromptCache) != "enabled":
            return None
        return self._options.get(Options.Agent.Anthropic.PromptCacheTTL)
