# Chat Pane: business vs. display logic

This is the working spec for the chat pane rewrite. The guiding principle
is **business logic in the VM, rendering logic in the View** — *not* "view
is a dumb mirror of the VM." The View is allowed to hold state (focus,
scroll position, raw editing state, key bindings); it just cannot make
business decisions.

We are **not** chasing feature parity with the current `chat_pane.py`. The
plan is to land a minimal working pane and port features over incrementally
until we're ready to swap. Some ports will require building out new sub-VMs
first (e.g. `OptionsEditorViewModel`); those count as deductions from the
new chat pane's scope until they land.

One design constraint we keep from the uppercase spec: **sub-VMs live
inside the feed.** Things like `CommitProposalViewModel` are appended into
the same ordered list as chat messages. The View's discovery pass turns
each feed entry into the appropriate widget; sub-VMs become focusable
child widgets bound to their own VM.

---

## View-Model — business logic

### Feed

The ordered list of feed entries. Each entry is either a `ChatMessageData`
or a sub-VM reference (`CommitProposalViewModel`,
`AgentMessageHarnessViewModel`, `FlashcardReviewViewModel`, …). Append-only
in the common case; the VM's API mutates it.

Includes:
- The consecutive-system-message dedup rule (post-dedup → fire ping on
  prior entry instead of appending).
- Routing of appended user/system messages to `AgentSession` (skipped
  when `ui_only=True`).
- Mode-stamping each entry on insert.

### Input area

- `enabled: bool` — whether the input area accepts submissions.
- `hint: str` — placeholder text.
- `submit_target: SubmitTarget` — `CHAT` or `COMMIT_INSTRUCTIONS`. Decides
  what `submit_user_input()` does.
- `buffer: str` — the committed buffer text. The View calls
  `set_user_input_buffer(text)` on every change so the VM can derive
  command-palette state. Raw editing state (cursor, IME, etc.) stays in
  the View's `ChatInput`.
- User message history (the list itself). Recall navigation is a View
  affordance that calls VM setters; the list lives here so it survives
  across re-mounts.

### Command palette

Derived from `buffer`:
- `visible: bool` — true when buffer starts with `/`.
- `filter: str` — the post-`/` prefix used for filtering.

If this grows (categories, hover help, etc.) it gets its own sub-VM. For
now, flat fields on the parent.

### Session mode

- `session_mode: Mode` (IDLE / LEARN / REVIEW).
- Transition rules: the full (source × agent_busy × silent) matrix from
  the current widget's `_set_mode`. Funnels through one method.

### Active topic

- `active_topic: Topic | None`, `topic_path: list[str]`.
- Side effects of setting it (post system message, dismiss explorer if
  open) are business and live here.

### Agent run state

- `agent_run_state: RunState` (IDLE / RUNNING / CANCELLING).
- Start/cancel/lifecycle hooks. The VM owns the *abstraction* "is a run
  in flight"; the View owns the worker handle.
- Command gating: `_AGENT_GATED_COMMANDS` blocks `/commit`, `/options`
  while running.

### Sub-VM composition (children in feed)

The VM creates sub-VMs and appends them into the feed. The View's
discovery pass turns them into widgets.

- **Multi-instance, agent-emitted in feed**: `CommitProposalViewModel`,
  `FlashcardProposalViewModel`, `FlashcardReviewViewModel`,
  `AgentMessageHarnessViewModel`.
- **Singleton modals** (when ported): `ExplorerViewerViewModel`,
  `OptionsEditorViewModel` — also live in the feed when active, but the
  VM keeps a singleton slot and refocuses instead of creating a second.
- **Long-lived hidden** (when ported): `ResourceViewerViewModel` —
  persists across show/hide so its internal state survives toggling.

Universal teardown: when a sub-VM signals resolution, the parent removes
it from the feed and any singleton slot. No per-kind dismiss handler.

### Commit overlay state (deferred — see scope below)

When ported: phase machine (INACTIVE / SELECTING / AWAITING_INSTRUCTIONS
/ SUBMITTED), selectable indices, cursor, selected set, payload building,
subagent-vs-inline routing.

### Resource Manager

`ResourceManager` lives on the VM — it's a pure data access object.

### Command dispatch

Parsing slash and shell commands, command registry, gating against
`agent_run_state`. Handlers are VM methods; they touch the feed,
sub-VMs, and intents — never Textual.

### Verbosity-hint throttle (deferred)

10-minute window logic, when ported.

### App-shell intents

Outbound queue for things only the app shell can do: `exit`,
`open_new_tab`, `push_screen`, `subprocess_editor`,
`copy_to_clipboard`, `notify`. View drains after each `dirty`.

---

## View — display logic

### Layout

`compose()`, mount/unmount, CSS, scroll behavior, the feed's
`VerticalScroll`, dock containers.

### Status bar

Pure projection over VM state (mode, topic path, token usage, model name,
verbosity). Re-read on every `dirty`.

### Dock areas

The slot containers and how docked content looks. The *current dock id*
(when the resource viewer is ported) is VM state — the cycle is one line
of business logic — but the rendering of each slot is the View.

### Navigation

Focus traversal between input area and focusable feed children
(Ctrl+Up/Down). The View maintains its own ordered focus list, built by
walking the feed and picking out focusable entries. The VM exposes feed
order; the View derives focus order.

In commit mode (when ported), Ctrl+Up/Down mutates `commit.cursor`
instead of moving focus — that's the View's key handler calling a VM
method, not the VM owning a focus stack.

### Rendering choices

Markdown vs. rich, ping animation, spinner, ☐/☑ glyphs, commit-decoration
CSS classes, scroll-end snap.

### Worker scheduling

Textual workers, `app.suspend()`, subprocess invocation. The VM exposes
`start_agent_run()` / `cancel_run()` and a callback surface; the View
wires it to a worker.

### Raw input editing

The `ChatInput` widget's internal text editing, cursor position,
history-recall keybinds. The View calls into the VM on commit and on
submit.

---

## Initial feature set

The plan is **steps 1–3 only**, then re-evaluate. We get a working agent
loop first; everything else (status bar, commit, explorer, options,
flashcards, disambiguation, verbosity hint, shell commands) is deferred.

### Step 1 — Feed + ordinary chat messages

- `Feed` with `ChatMessageData` entries; consecutive-system dedup +
  ping signal. (The ping path is wired but unreachable from this
  step's UI alone — user messages always interleave the system
  echoes. Step 2 exercises it when slash commands post repeated
  system notifications.)
- Input area: enabled/hint/buffer; submit appends a user message and
  a stub system echo of it.
- View: append-driven mount (`feed_append` callback), scroll-to-end
  on append, ping forwarded to existing widgets, basic focus
  handling (input focused on mount). A full discovery pass lands in
  step 3 when sub-VMs enter the feed.

### Step 2 — Slash commands + session mode  *(landed)*

- `CommandPaletteViewModel` as a sub-VM owned by the chat pane VM —
  decoupled, with its own dirty channel. The parent pushes the input
  buffer through `update_for_input(text)`; the palette owns
  visibility, filter, cursor, and `selected_command`.
- `CommandRegistry` (reused from `rhizome.tui.commands`) lives on the
  parent VM. Inline command handlers register on construction; each
  one calls the same public VM surface a non-command caller would
  use (`clear_feed`, `set_session_mode`, `append_message`).
- Commands shipped: `/clear`, `/idle`, `/learn`, `/review`, `/echo`.
- `set_session_mode(mode)` handles the user-initiated, agent-idle
  branch of the legacy transition matrix. The agent-busy / source=agent
  branches come in step 3.
- Dispatch: `submit_user_input` branches on a leading `/`, spawning
  `_execute_command(text)` as a background task. Errors and help
  text from click come back as `ERROR` / `SYSTEM(rich=True)` messages.
- View additions: `CommandPalette` widget mounted below the input,
  bound to the palette sub-VM; `_command_registry` passthrough so
  `ChatInput._is_complete_command` (now duck-typed) can reach it.

### Step 3 — Agent session bootstrap  *(landed, minimal)*

- VM takes `session_factory` at construction; builds a
  `ResourceManager` eagerly and exposes `agent_session: AgentSession |
  None`.
- `bootstrap_agent_session(app_options, *, debug)` constructs the
  session lazily from app options. The view's `on_mount` is the only
  caller (it has access to `self.app.options`).
- Nothing else is wired yet — no worker, no streaming, no harness,
  no command gating, no agent-busy mode transitions. Held but unused.

### Step 3b — Agent worker + AgentMessageHarness in feed *(future)*

- `AgentMessageHarnessViewModel` as the first feed-resident sub-VM.
- `start_agent_run()` / `cancel_run()` / terminal callbacks; the View
  runs the worker.
- Completes the (source × agent_busy) half of the session-mode matrix.

### Deferred (not in initial scope)

Active topic + status bar projection, commit-selection overlay,
`CommitProposalViewModel` in feed (already built — port after step 3),
resource viewer + docking, explorer, options editor + subprocess
editor, flashcard proposal/review, disambiguation
(`Choices`/`MultipleChoices`), verbosity-hint throttle, `!` shell
commands, ping-pulse animation polish.

---

## Decisions locked in

- **Buffer ownership**: VM owns the committed buffer text; View pushes
  on change.
- **Focus traversal**: View owns the active-widget stack and derives
  focus order from feed order.
- **Input mode shape**: `enabled` + `hint` + `submit_target` — no
  4-value enum. Disabled-for-interrupt and disabled-for-choices collapse
  to `enabled=False` + a different `hint`.
- **Sub-VMs live in the feed**: feed entries are either messages or
  sub-VM references; the View's discovery pass materializes widgets.
- **No feature parity**: incremental port; some destinations need new
  sub-VMs built first.
