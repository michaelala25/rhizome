# ChatPaneViewModel

Headless model for the `ChatPane` widget. The widget today is a 2000-line
monolith that simultaneously manages: the message log, the agent worker, the
session mode, the active topic, slash-command dispatch, the chat-input bar's
several modes, the command palette, a stack of "active widgets" for Ctrl+Up/Down
navigation, the dock layout for the resource viewer, the commit-selection
overlay, and the verbosity-hint throttle. The goal of the VM extraction is to
pull all of the *state* and *transitions* out of the Textual widget so that the
view layer becomes a flat projection that mounts/unmounts and styles based on
what the VM exposes.

This is by far the largest VM in the project. Unlike `CommitProposal` /
`FlashcardProposal` / `ExplorerViewer` (which are modal, dismissable widgets
with a single resolution), `ChatPane` is the *root session widget* — it has
no resolution and lives for the lifetime of the tab. It is also the *host* for
several of those modal sub-widgets, so a chunk of its surface is "manage the
sub-VM I'm currently displaying."

> **Naming note.** Throughout this doc, "VM" alone means `ChatPaneViewModel`.
> Sub-widget VMs are named explicitly (`ExplorerViewerViewModel`, etc).

============================================================================================
## Hierarchy contract (foundational)
============================================================================================

The parent–child coordination between any VM and its view follows one principle:

> **Children are created by the VM. The view discovers them on its next refresh
> and materializes the corresponding child widgets, each bound to its child VM.
> Each child widget thereafter owns its own VM ↔ view binding and refreshes off
> its own VM's `dirty`. The parent view only re-runs its discovery pass when
> the parent VM signals `dirty`.**

Corollaries that follow without extra machinery:

- The parent view keeps a small map of "child VMs I've already mounted" so each
  refresh is a diff, not a remount.
- Reaping is the inverse: when the parent VM drops a child VM reference, the
  next parent refresh sees it's gone and unmounts.
- Multi-instance vs. singleton is purely a question of how the *parent VM*
  stores its children (a list, a dict, or a single attribute) — the view
  doesn't care.
- Sub-sub-VMs work because the principle is recursive: a child widget is
  itself a "parent view" for its own children.

Throughout the rest of this spec, whenever we say the VM "creates a sub-VM"
or "drops a sub-VM," that's the only contract being invoked. The view's
discovery pass and widget construction are not re-specified per case.

Sketch of the discovery pass (illustrative — concrete shape is the view's
business, not the VM's):

```python
def _refresh(self) -> None:
    seen: set[int] = set()
    for sub_vm in self.vm.iter_children():     # any iterable order works
        seen.add(id(sub_vm))
        if id(sub_vm) not in self._mounted:
            widget = self._build_child_widget(sub_vm)
            self._mount_child(widget, sub_vm)  # view picks the container
            self._mounted[id(sub_vm)] = widget
    for vm_id in list(self._mounted) - seen:
        self._mounted.pop(vm_id).remove()
```

`iter_children()` is a deliberately loose VM contract — the parent VM may
expose children as a list, several typed attributes, or a dict; the view
adapts. Reaping uses identity, not key equality, so swapping a sub-VM for a
fresh instance of the same kind is a remount, as expected.

============================================================================================
## Scoping — what's in, what's out
============================================================================================

### In scope (owned by ChatPaneViewModel)

- **Message log.** The append-only list of `ChatMessageData` plus the
  consecutive-system-message dedup rule (which today peeks at the rendered
  widget tree to ping the prior message — the VM moves that decision to a model
  predicate; the view does the ping animation).
- **Session mode.** `Mode.IDLE / LEARN / REVIEW` and the rules for transitioning
  it, including the four-way matrix of (source × agent_busy) that today lives
  in `_set_mode` (see "Session-mode transitions" below).
- **Active topic.** `Topic | None` and the cached `topic_path: list[str]` for
  status-bar rendering.
- **Agent run state.** The `_agent_busy` flag, plus the lifecycle hooks fired
  when a run starts / streams / cancels / errors / finalizes. The VM does NOT
  own the worker handle (Textual concept); it owns the *abstraction* of "is a
  run in flight" and a single `cancel_run()` method that the view wires to the
  worker.
- **Commit-selection overlay state.** The selectable-message indices, cursor,
  selected set, and the small phase machine `INACTIVE → SELECTING →
  AWAITING_INSTRUCTIONS → SUBMITTED`. (Today this is the `CommitState`
  dataclass plus a pile of methods on `ChatPane`. We promote the dataclass into
  a proper sub-VM owned by the parent.)
- **Chat-input mode.** A small enum describing which input bar is active and
  how (main / commit-instructions / disabled-pending-interrupt /
  disabled-pending-choices). The VM owns the placeholder string and the
  enable/disable bit; the view binds them onto the `ChatInput` widget.
- **Resource-viewer presence + dock id.** Whether the resource viewer is
  currently docked, and at which `dock-{bottom,left,right}` slot (Ctrl+D
  cycles). The viewer's *internal* state continues to live on
  `ResourceViewerViewModel`, which the parent VM holds a reference to.
- **Active sub-widget stack.** The Ctrl+Up/Down navigation list. In the VM
  this is an ordered list of sub-VM references (the same identities the view
  uses for discovery). The view materialises focus.
- **Sub-VM composition.** The parent VM creates and disposes child sub-VMs
  per the hierarchy contract above (`ExplorerViewerViewModel`,
  `CommitProposalViewModel`, `FlashcardProposalViewModel`,
  `FlashcardReviewViewModel`, `OptionsEditorViewModel`). The VM picks
  whatever storage shape it wants per kind — a singleton attribute for
  modals like the explorer, a list for repeatable artifacts like
  flashcard-review and commit-proposal widgets emitted by the agent. The
  parent VM is also responsible for routing top-level events (DB-commit
  notifications, for instance) to whichever sub-VMs are live.
- **Verbosity-hint throttle.** The `_verbosity_hint_allowed` flag and the
  `_verbosity_hint_last_shown` timestamp. The VM exposes a "should I notify?"
  predicate so the view stays dumb.
- **Command dispatch.** The command registry, the agent-busy gating set, and
  the parsed-input → command-or-chat decision. Each command handler stays in
  the VM as an `async def`. The handlers do *not* touch Textual; their effects
  go through the VM's own surface (append a message, mount a sub-VM, set a
  flag). Concretely, `_cmd_explore` becomes "create an
  `ExplorerViewerViewModel`, install it on `self.explorer`, push it on the
  active-widget stack, set the input placeholder."

### Out of scope (the view keeps these)

- **Textual layout, CSS, child mounting.** All `compose()` / `mount()` /
  `query_one(...)` lives in the view. The VM never speaks about
  `#message-area` or `VerticalScroll`.
- **Worker scheduling.** The view wires the agent run into Textual's worker
  system; the VM exposes `start_agent_run()` / `cancel_run()` and a callback
  surface (see "Agent run protocol").
- **Spinner / scroll-end / animation.** All visual artifacts.
- **Markdown vs. rich rendering.** The VM stores `ChatMessageData` (with the
  `rich` flag); the view picks the widget class.
- **Commit-mode key handling.** The view's `on_key` reads the VM's
  `commit.is_selecting` and routes ↑/↓/Space/Ctrl+J/Esc into VM methods; the
  VM defines the methods, not the keymap.
- **Subprocess and editor invocations** (`/options --edit`'s `subprocess.run`
  + `app.suspend()`). The VM exposes `cmd_options_edit()` which returns a
  `OptionsEditRequest` payload for the view to execute; the view calls back
  into `apply_options_edit_result(text)` once the editor exits. This keeps the
  VM testable without spawning a subprocess.
- **`app.exit()`, `app.push_screen(...)`, tab open/close, log tab open.**
  These are app-shell concerns; the VM signals them via a small `intents`
  surface (`exit`, `open_new_tab`, `open_logs_tab`, `close_tab`,
  `push_new_resource_screen`) rather than calling the Textual app. The view
  drains intents on each `dirty`.

### Deliberately deferred (do later, not now)

- **Splitting the agent session itself.** `AgentSession` is already mostly
  headless; we keep it as-is and let the parent VM hold a reference. The two
  callbacks it currently fires into the widget (`on_token_usage_changed`,
  `on_rebuild_agent`) become VM methods. We do *not* refactor `AgentSession`
  in this pass.
- **Pulling `ChatInput` and `CommandPalette` out of Textual.** They stay
  Textual widgets; the VM only owns the *mode* / *placeholder* / *filter
  text*, not the editing state.
- **Promoting the message log into its own sub-VM.** Tempting (the dedup +
  `ui_only` + role-based agent routing lives there) but it's not used by any
  other widget, so we keep it inline on `ChatPaneViewModel` for v1.

============================================================================================
## State machine
============================================================================================

`ChatPaneViewModel` has *several* orthogonal sub-states rather than a single
top-level enum. Each axis transitions independently; the view is the only
place that combines them visually. The axes are:

    ┌─────────────────────┬───────────────────────────────────────────────┐
    │ axis                │ values                                        │
    ├─────────────────────┼───────────────────────────────────────────────┤
    │ session_mode        │ IDLE / LEARN / REVIEW                         │
    │ agent_run_state     │ IDLE / RUNNING / CANCELLING                   │
    │ chat_input_mode     │ MAIN / COMMIT_INSTRUCTIONS /                  │
    │                     │ DISABLED_INTERRUPT / DISABLED_CHOICES         │
    │ commit_phase        │ INACTIVE / SELECTING / AWAITING_INSTRUCTIONS /│
    │                     │ SUBMITTED                                     │
    │ resource_viewer     │ HIDDEN / VISIBLE@<dock_id>                    │
    └─────────────────────┴───────────────────────────────────────────────┘

Children (sub-VMs) are not a state-machine axis — they're a collection
governed by the hierarchy contract. Their presence/absence is observable
state, but the parent doesn't track an enum over it.

The legal-combination matrix is constrained but not encoded as a single state
type (that would explode). Instead, each transition method asserts the
preconditions it cares about. For example:

- `start_agent_run()` asserts `agent_run_state == IDLE`.
- `enter_commit_mode()` asserts `commit_phase == INACTIVE` and
  `agent_run_state == IDLE`.
- `cmd_options(...)` and `cmd_commit(...)` are gated on `agent_run_state ==
  IDLE` (the `_AGENT_GATED_COMMANDS` set today).

### Session-mode transitions

Faithful translation of `_set_mode(mode, *, silent, source)`. Inputs:

    target_mode    ∈ {IDLE, LEARN, REVIEW}
    silent         ∈ {True, False}
    source         ∈ {"user", "agent"}     # agent => agent tool call
    agent_busy     ∈ {True, False}

Behaviour:

1. If `source == "agent"`: assert `agent_busy`. Force `silent = True`.
2. If `target_mode == session_mode`:
    - If not silent, append a "Already in <mode> mode." system message.
    - Return; no further work.
3. If `source == "agent"`:
    - Set `session_mode = target_mode`.
    - Fire status-bar dirty.
    - Clear any pending user-initiated mode change on the agent middleware
      (agent tool wins over a queued user toggle).
    - Done.
4. From here on `source == "user"`. Compose the announcement string
   (`"Returned to idle mode."` / `"Entered <mode> mode."`).
5. If `agent_busy`:
    - Set `session_mode = target_mode`.
    - Queue a pending-user-mode change on the agent middleware (so the next
      model invocation sees it).
    - If not silent, post the announcement to the UI message log *only*
      (`ui_only=True`); the agent will see the system notification when the
      pending change drains, not via the VM.
6. Else (`agent_busy == False`):
    - Set `session_mode = target_mode`.
    - If silent: push the announcement to the agent as a system notification
      (no UI message).
    - Else: append a normal system message (which goes to both UI and agent).
7. Fire status-bar dirty.

The VM exposes this as `async set_session_mode(mode, *, silent=False,
source="user")`. The four call sites (`/idle`, `/learn`, `/review`,
shift+tab `cycle_mode`, and the agent's `set_mode` tool) all funnel through
it.

### Agent run state

    IDLE ─ start_agent_run() ──► RUNNING
                                   │
                                   ├─ on_run_token_usage_changed   (in-state)
                                   ├─ on_run_message(msg)          (in-state)
                                   ├─ on_run_interrupt_pending(w)  (in-state)
                                   ├─ on_run_interrupt_resolved()  (in-state)
                                   │
                                   ├─ finish_run_success() ──► IDLE
                                   ├─ finish_run_cancelled() ──► IDLE
                                   └─ finish_run_error(exc) ──► IDLE

    RUNNING ─ cancel_run() ──► CANCELLING ─ finish_run_cancelled() ──► IDLE

The view starts a Textual worker on `start_agent_run()` and calls back
`finish_run_*` from the worker's `try/except/finally`. `cancel_run()` is the
only state-changing edge that the *view* triggers without the VM having
initiated it; the view first transitions the VM to `CANCELLING`, then cancels
the worker.

### Chat-input mode

    MAIN
        ├─ enter_commit_instructions()      ──► COMMIT_INSTRUCTIONS
        ├─ disable_for_interrupt(widget_id) ──► DISABLED_INTERRUPT
        └─ disable_for_choices(widget_id)   ──► DISABLED_CHOICES

    COMMIT_INSTRUCTIONS
        ├─ submit_commit_instructions(text) ──► MAIN  (fires _submit_commit)
        └─ cancel_commit_instructions()     ──► MAIN

    DISABLED_INTERRUPT
        └─ resolve_interrupt()              ──► MAIN

    DISABLED_CHOICES
        └─ resolve_choices()                ──► MAIN

The VM also owns `placeholder_text: str` and `disabled: bool`, both derived
from `chat_input_mode`. The view binds them onto the `ChatInput` instance.

### Commit-selection phase

    INACTIVE
        └─ enter_commit_mode(selectable_indices) ──► SELECTING
              [no-op + system message if no selectable messages]

    SELECTING
        ├─ commit_move_cursor(±1)            (in-state)
        ├─ commit_toggle_current()           (in-state; auto-advances cursor)
        ├─ confirm_commit_selection()
        │     ├─ if selected is empty ──► INACTIVE  (system message, restore input)
        │     └─ else ──► AWAITING_INSTRUCTIONS  (chat_input_mode → COMMIT_INSTRUCTIONS)
        └─ cancel_commit_selection() ──► INACTIVE

    AWAITING_INSTRUCTIONS
        ├─ submit_commit_instructions(text) ──► SUBMITTED  (fires payload + agent run)
        └─ cancel_commit_instructions()     ──► INACTIVE

    SUBMITTED
        ├─ on_commit_approved(count) ──► INACTIVE  (clears overlay, system msg)
        └─ on_commit_cancelled()     ──► INACTIVE

The VM keeps the per-message `cursor`, `selected: set[int]`, and the
working `selectable: list[int]` of indices into the message log. The decoration
class names (`--commit-selectable`, `--commit-cursor`, `--commit-selected`,
`commit-checkbox`) and the ☐/☑ glyph are entirely view concerns; the VM
exposes `is_selectable(idx) / is_cursor(idx) / is_selected(idx)`.

### Children (sub-VMs)

Per the hierarchy contract, children are a collection on the parent VM, not
a state axis. Each *kind* of child picks its own storage shape based on
whether multiple instances are meaningful:

- **Singleton kinds** (explorer, options editor): a single
  `Optional[SubVM]` attribute. Command handlers refuse to create a second
  one and instead refocus the existing.
- **Multi-instance kinds** (commit proposal, flashcard proposal,
  flashcard review): a list of sub-VMs. Each instance is independent;
  many can coexist in the message log.
- **Long-lived hidden kinds** (resource viewer): the sub-VM is held
  permanently on the parent (`resource_viewer_vm`). It enters the
  child set only while visible; on hide it's removed from the visible
  children but the VM persists, preserving its internal state across
  show/hide cycles.

`iter_children()` walks all of these and yields the currently-visible
sub-VMs in mount order. That's what the view's discovery pass consumes.

============================================================================================
## Stateful attributes
============================================================================================

### Message log

    messages: list[ChatMessageData]
        Full append-only history. Mode-stamped on insert (each entry's `mode`
        field captures the session_mode at append time).

    _last_message_role: Role | None
        Cached for the consecutive-system-message dedup rule.

### Session

    session_mode: Mode                          # IDLE / LEARN / REVIEW
    active_topic: Topic | None
    topic_path: list[str]                       # root → leaf, exclusive of synthetic root

### Agent

    agent_session: AgentSession                 # constructed in bootstrap()
    agent_run_state: RunState                   # IDLE / RUNNING / CANCELLING
    _pending_agent_run_request: AgentRunRequest | None
        Set when a VM command queues a run; consumed by the view's
        `start_agent_worker` callback. (See "Agent run protocol".)

### Options

    options: Options | None                     # session-scoped, parented to app.options
    _verbosity_hint_allowed: bool
    _verbosity_hint_last_shown: float           # monotonic seconds; 600s window

### Commit overlay

    commit: CommitOverlayState
        .phase: INACTIVE / SELECTING / AWAITING_INSTRUCTIONS / SUBMITTED
        .selectable_indices: list[int]          # indices into `messages`
        .cursor: int
        .selected: set[int]
        .pending_payload: list[CommitEntryPayload] | None
            Set on confirm, used by `_submit_commit` to compute routing
            (subagent vs. inline) and inject into agent state.

### Children (sub-VMs)

    # Singleton modals
    explorer_vm: ExplorerViewerViewModel | None
    options_editor_vm: OptionsEditorViewModel | None

    # Multi-instance, agent-emitted artifacts
    commit_proposals: list[CommitProposalViewModel]
    flashcard_proposals: list[FlashcardProposalViewModel]
    flashcard_reviews: list[FlashcardReviewViewModel]

    # Long-lived hidden
    resource_viewer_vm: ResourceViewerViewModel       # always non-None
    resource_viewer_visible: bool                     # gates inclusion in iter_children

    def iter_children() -> Iterable[SubVM]:
        Yields whichever of the above are currently visible, in mount order.
        Consumed by the view's discovery pass.

    _active_widget_stack: list[SubVM]
        References (not opaque keys) to sub-VMs in Ctrl+Up/Down navigation
        order. The view maps each entry to its mounted widget for focus.

### Resource viewer

    resource_manager: ResourceManager
    resource_viewer_vm: ResourceViewerViewModel
        Long-lived; outlives mount/unmount cycles.
    resource_viewer_visible: bool
    resource_viewer_dock_id: Literal["dock-bottom", "dock-left", "dock-right"]

### Chat input

    chat_input_mode: InputMode
    chat_input_placeholder: str                 # derived; cached for cheap reads
    chat_input_disabled: bool                   # derived

### Command dispatch

    command_registry: CommandRegistry
        Built once in `__init__`; commands close over `self`.
    _AGENT_GATED_COMMANDS: ClassVar[set[str]] = {"commit", "options"}

### View signalling

    dirty: list[Callable[[], None]]
        Single observer list. Fired exactly once per public mutation
        (synchronous methods) or once per discrete async-step (async
        methods that span multiple awaits — see per-method notes).

    intents: list[Intent]
        Side-effect requests for the view to drain after each `dirty`. Each
        intent is one of:
            ExitApp
            OpenNewChatTab
            OpenLogsTab
            CloseActiveTab
            PushScreen(NewResourceScreen)
            CopyToClipboard(text)
            Notify(text, severity)
            StartEditorSubprocess(initial_text, callback_token)
        The view drains the list after handling `dirty`. The VM never calls
        Textual or `app` directly.

============================================================================================
## Public API
============================================================================================

### Lifecycle

    async bootstrap(app_options: Options)
        Constructs `self.options` parented to `app_options`; constructs
        `self.agent_session` with the resolved provider/model/agent_kwargs;
        wires post-update subscriptions; emits a single `dirty`.

    async shutdown()
        Detaches `self.options`. Cancels any in-flight agent run via
        `cancel_run()`. Drops sub-VMs.

### Message log

    append_message(msg: ChatMessageData, *, ui_only: bool = False) -> None
        - Stamps `msg.mode = self.session_mode`.
        - Applies the consecutive-system dedup rule (if the previous appended
          message is a system message with identical content, the VM emits a
          *ping* signal and does NOT append). The view shows the ping; the
          VM models it as a `MessagePinged(idx)` notification on `dirty`-1
          listeners. (Practically this can be modeled as a flag on the
          last entry.)
        - If not `ui_only` and the agent_session exists, routes user-role
          messages through `agent_session.add_human_message(...)` and
          system-role messages through `agent_session.add_system_notification(...)`.
        - Fires `dirty`.

    clear_messages() -> None
        Empties `messages`; resets the active widget stack;
        fires `dirty`.

### Session mode

    async set_session_mode(mode, *, silent=False, source="user")
        See "Session-mode transitions" above. Fires `dirty` once.

### Active topic

    set_active_topic(topic: Topic | None, path: list[str]) -> None
        Updates `active_topic` and `topic_path`. Forwards to
        `resource_viewer_vm.set_active_topic(...)`. Posts a system message
        ("Selected topic: <name>" / "Cleared active topic"). If the
        explorer modal is live, dismisses it (sets `explorer_vm = None`,
        pops it from the stack, returns input mode to MAIN). Fires `dirty`.

### Agent run protocol

    start_agent_run() -> None
        Asserts `agent_run_state == IDLE`. Builds an `AgentRunRequest`
        capturing `(mode=session_mode.value, topic_name=...)` and the
        tool-use-visibility option, sets it on
        `_pending_agent_run_request`, transitions to `RUNNING`. Fires `dirty`.

        The view drains `_pending_agent_run_request` and starts a Textual
        worker that invokes `agent_session.stream(...)`, plumbing the
        stream's per-message / per-update / per-interrupt callbacks back
        into the VM via:

            on_run_message(msg: ChatMessageData)
            on_run_interrupt_pending(widget_id: str)
            on_run_interrupt_resolved()
            on_run_token_usage_changed()

        On the worker's terminal edge, the view calls one of:

            finish_run_success(body: str | None)
            finish_run_cancelled(body: str | None)
            finish_run_error(exc: BaseException)

        Each transitions to `IDLE`, appends the appropriate message
        (agent-final body / "(user cancelled)" / error), and fires `dirty`.

    cancel_run() -> None
        If `agent_run_state == RUNNING`, transitions to `CANCELLING` and
        emits an intent telling the view to cancel its worker. The terminal
        callback `finish_run_cancelled` returns to `IDLE`.

    on_agent_rebuilt(old_model: str, new_model: str) -> None
        Posts the system "Model changed to ..." message.
        Fires status-bar dirty.

### Verbosity hint throttle

    should_show_verbosity_hint() -> bool
        Reads `_verbosity_hint_allowed` and the 10-minute window. If yes,
        flips the flag to False and stamps `last_shown`. The view calls
        `notify(...)` on a true return.

### Commit overlay

    enter_commit_mode(selectable_indices: list[int]) -> None
    commit_move_cursor(delta: Literal[-1, +1]) -> None
    commit_toggle_current() -> None
    confirm_commit_selection() -> None
    cancel_commit_selection() -> None

    submit_commit_instructions(text: str) -> None
        Builds the payload (entries from selected indices, plus the
        preceding-user-message context for agent-only selection levels per
        the `commit_selectable` option), computes routing (subagent
        threshold), injects payload via `agent_session.set_commit_payload`,
        adds the system notification telling the agent which path to take,
        and finally calls `start_agent_run()`. Transitions
        `commit.phase = SUBMITTED`. Fires `dirty`.

    cancel_commit_instructions() -> None

    on_commit_approved(count: int) -> None
    on_commit_cancelled() -> None
        Both transition `commit.phase` back to `INACTIVE`. The first appends
        the "Committed N entry/entries" system message.

### Resource viewer

    async toggle_resource_viewer() -> None
        Flips `resource_viewer_visible`. The sub-VM persists either way.
    async cycle_resource_viewer_dock() -> None
        Cycles `resource_viewer_dock_id` through the three legal values.
        The view's discovery pass treats a slot change as a remount.
    on_resource_viewer_dismissed() -> None
        Folds into the universal `_reap_if_resolved` path: sets
        `resource_viewer_visible = False`. The VM is *not* dropped.

### Children (sub-VMs)

Per the hierarchy contract, every "open a modal / spawn an artifact" path
follows the same shape:

1. Construct the child VM.
2. Place it in the appropriate parent attribute (singleton slot or list).
3. Subscribe a parent listener on the child's `dirty` that calls
   `_reap_if_resolved(child)` — this is the universal teardown edge; no
   per-kind `on_<x>_dismissed` handler is required.
4. Push the child onto `_active_widget_stack` if it's focusable.
5. Apply any side state (e.g. chat-input placeholder override).
6. Fire `dirty`.

`_reap_if_resolved(child)` removes the child from whichever attribute
holds it, removes it from the active-widget stack, restores any side
state owned by that kind, and fires `dirty`. The view's next discovery
pass unmounts the corresponding widget.

Concrete entry points (one per command surface, not one per kind):

    async cmd_explore() -> None
    async cmd_options(*, edit: bool, scope: str) -> None
    async cmd_commit(*, auto: bool, instructions: str) -> None
    def emit_commit_proposal(entries, topic_map) -> None      # agent-side
    def emit_flashcard_proposal(flashcards) -> None           # agent-side
    def emit_flashcard_review(cards) -> None                  # agent-side

Bootstrapping. Children that need an async load (e.g.
`ExplorerViewerViewModel.bootstrap()`) are *not* awaited by the parent.
The parent only constructs and registers; the child widget calls
`bootstrap()` on its `on_mount` since that's where a worker context
exists.

### DB-commit notification

    async notify_database_committed(changed_tables: set[str]) -> None
        Iterates `iter_children()` and forwards the call to every
        data-displaying sub-VM that exposes its own
        `notify_database_committed`. Each sub-VM does its own
        cache-invalidation per its spec and emits its own `dirty`. The
        parent does not emit.

### Chat input

    set_input_mode(mode: InputMode) -> None
        Direct setter for tests and the resolve-* paths.

    submit_chat_input(text: str) -> None
        The dispatch entry point. Branches on text:
            - empty                   → no-op
            - starts with `!`         → handle_shell_command(text[1:])
            - parses to a `Command`   → handle_command(name, args)
            - else                    → handle_chat(text)
        Implements the `_AGENT_GATED_COMMANDS` gate.

    update_palette_from_input(text: str) -> None
        Updates `palette_filter_text` and `palette_visible`. The view
        binds these onto `CommandPalette`.

### Status-bar projection

    @property status_bar: StatusBarProjection
        Pure-data dataclass with: mode, topic_path, token_usage,
        model_name, verbosity. The view re-reads on every `dirty`.

============================================================================================
## VM contracts
============================================================================================

- `dirty` fires exactly once per public synchronous method, and once per
  *discrete* async step in async methods (typical pattern: emit on entry to
  schedule a load, emit again when the load resolves).
- The VM **never** imports from `textual.*` or `rhizome.tui.widgets.*` (only
  from sub-VMs and from `rhizome.tui.types`, `rhizome.tui.commands`,
  `rhizome.tui.options`, `rhizome.agent`, `rhizome.db`, `rhizome.resources`).
- Effects that need the app shell (worker scheduling, screen pushes, clipboard,
  subprocess) flow through the `intents` queue. The view drains intents on
  every `dirty`.
- Sub-VMs are first-class. The parent does not reach inside; it speaks to each
  sub-VM via that sub-VM's documented public surface and tears down on
  resolution.

============================================================================================
## Open questions / decisions to confirm
============================================================================================

The following are real choices, not bikeshed. Flagging so we can resolve before
implementing:

1. **One VM or several composed VMs?** I'm proposing one VM that *composes*
   sub-VMs, with the message-log + commit-overlay + chat-input-mode bundled
   into the parent. An alternative is to extract `MessageLogViewModel`,
   `CommitOverlayViewModel`, `ChatInputModeViewModel` as sibling sub-VMs that
   the parent holds. I think the bundle is right because none of those three
   have any reuse outside `ChatPane` and pulling them apart would mean the
   parent re-implements coordination glue (e.g. commit-overlay reads message
   indices, chat-input-mode reads commit-phase). But this is reversible.

2. **`notify_database_committed` emit policy.** Decided: each sub-VM emits
   its own `dirty`; the parent does not emit. Simpler and matches the
   hierarchy contract (children refresh independently of the parent).

3. **`AgentSession` callback shape.** Today `AgentSession` calls back into
   `ChatPane` via two hooks (`on_token_usage_changed`, `on_rebuild_agent`).
   The VM mirrors them as VM methods. We should *not* pass the VM-as-self to
   `AgentSession` — instead pass bound methods, so `AgentSession` stays
   ignorant of the VM type. (Same pattern that's there today.)

5. **Disambiguation widgets (`Choices` / `MultipleChoices`).** The
   `_resolve_identifiers` / `_disambiguate_identifiers` flow today is
   embedded in command handlers and `await`s `widget.wait_for_selection()`.
   In the VM, the analogous flow becomes: command handler awaits an
   abstract `IDisambiguationProvider.resolve(specs) -> answers` interface
   that the view implements by mounting `Choices`/`MultipleChoices` and
   resolving the future. This keeps the command handlers in the VM. Confirm
   we want the provider abstraction (vs. punting and keeping
   `_disambiguate_identifiers` in the view).

6. **`chat_input_mode` granularity.** I have four values
   (MAIN / COMMIT_INSTRUCTIONS / DISABLED_INTERRUPT / DISABLED_CHOICES);
   today the widget tracks placeholder + disabled directly. Four enum
   values feel right but there may be cases I missed (e.g. the explorer's
   "Ctrl+l to refocus chat input" placeholder is just a placeholder change,
   not a mode change — currently the spec treats it as an in-MAIN
   placeholder override). Confirm.

7. **Where does `click`-based command parsing live?** It's `rhizome.tui.commands`
   which is already framework-neutral. The VM uses it directly. Good.

============================================================================================
## What this spec does NOT cover (yet)
============================================================================================

Pending follow-up specs once this one is approved:

- The exact wire format of every `Intent` variant.
- The `OptionsEditorViewModel` spec (today the editor still owns its own
  state; it'll need its own `VIEW_MODEL.md` like the other modals).
- The `MessageLogProjection` shape — what the view needs to render each
  message (role, body, mode, rich, ping-pulse counter).
- The interplay between `set_session_mode(source="agent")` and the agent
  graph's `Command` mechanism — we should diagram the four-corner matrix
  with concrete event traces.
