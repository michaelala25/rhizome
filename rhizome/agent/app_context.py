"""Live app settings shared by the conversation view and the agent.

``AppContextStore`` is the single source of truth for per-branch app settings — the active mode and the
answer-verbosity preference — while a conversation is live. It hangs off the agent context, so it follows
the same rules as the resource stores: a communication *channel*, not a checkpointed answer. The reference
is fixed for a run (langgraph's contract), but the object behind it mutates, and writers reach through it.

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

from rhizome.utils.callbacks import CallbackHost

VALID_MODES: tuple[str, ...] = ("idle", "learn", "review")
"""The agent-side mode vocabulary. The view's ``rhizome.tui.types.Mode`` enum carries the same values;
kept as bare strings here so the agent stack stays free of a TUI dependency, matching ``RootAgentState``."""

VALID_VERBOSITIES: tuple[str, ...] = ("terse", "standard", "verbose", "auto")
"""The answer-verbosity vocabulary, mirroring ``Options.Agent.AnswerVerbosity.choices``. Bare strings for
the same TUI-independence reason as ``VALID_MODES``."""


class AppContextStore(CallbackHost):
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

    def copy_from(self, other: "AppContextStore") -> None:
        """Adopt ``other``'s settings by value — branch seeding, mirroring ``ResourceContextStore``.
        Silent (no event): seeding a fresh child node is not a user/agent change."""
        self._mode = other._mode
        self._verbosity = other._verbosity
