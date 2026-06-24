"""App-context channels shared by the conversation view and the agent.

Two app→engine channels live here:

- ``LocalAppContextStore`` — per-branch (node-local) app *settings* (mode, verbosity): an SSOT that answers
  "what is the current value", written by both the user (view) and the agent (tools), diffed into
  checkpointed state by the prompt engine.
- ``AppContextHooks`` (+ the ``AppContextHookService`` DI protocol) — app-published *facts* about the
  environment, a SCOPED registry chained like ``OptionService`` (app → workspace): app-scope facts
  (host/render capabilities) live at the root and fall through to every workspace; workspace-scope facts
  (the active model) live on a child that shadows by key. ``register`` / ``unregister`` are scope-local; the
  engine reads the merged effective set via ``fragments()`` and renders it as ephemeral tail context each
  ``prepare`` — never checkpointed, so facts re-derive per process. No SSOT, answers no "what is the value"
  query — hence "hooks", not "store".

``LocalAppContextStore`` hangs off the agent context directly (a node-local channel); the resolved
(workspace-scoped) ``AppContextHooks`` is threaded onto the context by the conversation graph. Both follow
the resource-store rule: a communication *channel*, not a checkpointed answer — the reference is fixed for a
run (langgraph's contract), but the object behind it is live.

Mode has two writers, one cell:

- the **user** flips it from the view (e.g. shift+tab),
- the **agent** flips it via the ``set_mode`` tool, reaching this store through ``ctx.app_state``.

Both go through ``set_mode`` here, which emits ``OnModeChanged`` so a status bar reflects either path
immediately. The matching ``RootAgentState["mode"]`` is a separate, checkpointed snapshot written *only* by
the prompt engine, which diffs this store against it at compile time (the resource-store discipline: the
store carries desire, state carries the engine-committed fact). A tool must never write ``state["mode"]``
itself — a double write would erase the store-vs-state delta the engine relies on to react to a switch.

Verbosity rides the same channel for the view: the user flips it (e.g. ctrl+b), ``set_verbosity`` emits
``OnVerbosityChanged``, and the status bar reflects it. No prompt-engine consumer reads it yet — committing
it into ephemeral-context shaping the way mode is committed is a separate task.

Per-branch, like ``local_resources``: one instance per conversation node, seeded from the parent on a
branch via ``copy_from``.
"""

from typing import Callable, Protocol

from rhizome.utils.callbacks import CallbackHost

VALID_MODES: tuple[str, ...] = ("idle", "learn", "review")
"""The agent-side mode vocabulary. The view's ``rhizome.tui.types.Mode`` enum carries the same values;
kept as bare strings here so the agent stack stays free of a TUI dependency, matching ``RootAgentState``."""

VALID_VERBOSITIES: tuple[str, ...] = ("terse", "standard", "verbose", "auto")
"""The answer-verbosity vocabulary, mirroring ``Options.Agent.AnswerVerbosity.choices``. Bare strings for
the same TUI-independence reason as ``VALID_MODES``."""


class LocalAppContextStore(CallbackHost):
    """SSOT for live per-branch app settings (mode, verbosity). See the module docstring."""

    class Callbacks:
        OnModeChanged = "OnModeChanged"            # (old: str, new: str)
        OnVerbosityChanged = "OnVerbosityChanged"  # (old: str, new: str)

    def __init__(self, mode: str = "idle", verbosity: str = "auto") -> None:
        super().__init__()
        self._mode = mode
        self._verbosity = verbosity
        self.make_callback_groups({
            self.Callbacks.OnModeChanged: (str, str),
            self.Callbacks.OnVerbosityChanged: (str, str),
        })

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> bool:
        """Set the active mode; emits ``OnModeChanged(old, new)`` and returns whether it changed.
        Idempotent — setting the current mode is a silent no-op. Raises ``ValueError`` on an unknown
        mode (the backstop; agent input is pre-validated by the ``set_mode`` tool)."""
        if mode not in VALID_MODES:
            raise ValueError(f"Unknown mode {mode!r}. Valid modes: {', '.join(VALID_MODES)}.")
        if mode == self._mode:
            return False
        old, self._mode = self._mode, mode
        self.emit(self.Callbacks.OnModeChanged, old, mode)
        return True

    @property
    def verbosity(self) -> str:
        return self._verbosity

    def set_verbosity(self, verbosity: str) -> bool:
        """Set the answer verbosity; emits ``OnVerbosityChanged(old, new)`` and returns whether it
        changed. Idempotent — setting the current value is a silent no-op. Raises ``ValueError`` on an
        unknown verbosity (the backstop for direct callers)."""
        if verbosity not in VALID_VERBOSITIES:
            raise ValueError(f"Unknown verbosity {verbosity!r}. Valid: {', '.join(VALID_VERBOSITIES)}.")
        if verbosity == self._verbosity:
            return False
        old, self._verbosity = self._verbosity, verbosity
        self.emit(self.Callbacks.OnVerbosityChanged, old, verbosity)
        return True

    def copy_from(self, other: "LocalAppContextStore") -> None:
        """Adopt ``other``'s settings by value — branch seeding, mirroring ``ResourceContextStore``.
        Silent (no event): seeding a fresh child node is not a user/agent change."""
        self._mode = other._mode
        self._verbosity = other._verbosity


# ========================================================================================================================
# Service: AppContextHookService
#   Shape : protocol + first-party impl (AppContextHooks, below)
#   Scope : root (app) -> workspace (scoped; a child shadows its parent by key and merges for rendering)
# ========================================================================================================================

type ContextProducer = Callable[[], str | None]
"""An app-published fact: called at render time (every ``prepare``), returns the fact's current text or
``None`` to omit it this turn. Must be cheap and synchronous — it runs on every wire request."""


class AppContextHookService(Protocol):
    """Consumer + registrant facing slice of a scoped app-context hook registry: publish/withdraw a fact at
    this scope, and read the merged effective set the engine renders. Consumers depend on this protocol; the
    concrete ``AppContextHooks`` satisfies it and adds the scope plumbing."""

    def register(self, key: str, fact: "ContextProducer | str") -> None: ...
    def unregister(self, key: str) -> None: ...
    def fragments(self) -> list[str]: ...


class AppContextHooks(AppContextHookService):
    """Scoped registry of app-published environment facts, rendered as one tail ``<system-reminder>``.

    Scoped like ``OptionService`` / ``CommandRegistry``: a child created with ``parent=`` shadows the parent
    by key and merges for rendering, so app-scope facts (host/render capabilities) registered at the root
    reach every workspace, while workspace-scope facts (the active model) live on a child. ``register`` /
    ``unregister`` are scope-LOCAL — a child can't withdraw a parent's fact, only shadow it (register the
    same key, e.g. a producer returning ``None`` to hide it).

    The engine reads the merged effective set via ``fragments()`` and folds it into one ephemeral tail
    message (never checkpointed — facts re-derive per process). The app owns content and wording; the engine
    owns placement and caching. Not a ``CallbackHost``: nothing observes a change — the engine reads the
    current set when it renders. Producers are held by strong reference (a plain dict), unlike callback
    subscribers, because a fact must survive for as long as it is registered.
    """

    def __init__(self, parent: "AppContextHooks | None" = None) -> None:
        self._parent = parent
        self._producers: dict[str, ContextProducer] = {}   # THIS scope only; insertion-ordered

    def register(self, key: str, fact: ContextProducer | str) -> None:
        """Publish ``fact`` under ``key`` at THIS scope (shadowing a parent's same key). A bare string is a
        static fact; a callable is re-evaluated each render, so it can track live state — or return ``None``
        to hide itself this turn."""
        self._producers[key] = (lambda value=fact: value) if isinstance(fact, str) else fact

    def unregister(self, key: str) -> None:
        """Drop the fact at ``key`` from THIS scope (no-op if absent here). Scope-local: it cannot remove a
        parent-scope fact — shadow that with a local key returning ``None`` to hide it instead."""
        self._producers.pop(key, None)

    def __contains__(self, key: str) -> bool:
        """Whether ``key`` is registered at THIS scope (the parent chain is not consulted)."""
        return key in self._producers

    def _effective(self) -> dict[str, ContextProducer]:
        """The merged key→producer map: the parent chain first, then this scope shadowing by key (a child
        override keeps the parent's render position but supplies the new producer)."""
        merged = self._parent._effective() if self._parent is not None else {}
        merged.update(self._producers)
        return merged

    def fragments(self) -> list[str]:
        """The engine's read side: each effective producer's current text (parent facts first, then this
        scope's), dropping ``None`` and empty strings. The engine joins and wraps these, so this stays
        ignorant of prompt formatting."""
        out: list[str] = []
        for producer in self._effective().values():
            fragment = producer()
            if fragment:
                out.append(fragment)
        return out
