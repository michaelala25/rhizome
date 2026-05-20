# rhizome/tui/widgets/chat_pane/

Chat pane package: legacy widget + in-progress MVVM rewrite. The legacy
implementation in `_legacy.py` is the production code path; the MVVM
rewrite lives in `view.py` / `view_model.py` and a handful of
sub-component files. They run side-by-side until the swap is permanent
(toggled via the app's `--new-chat-pane` flag).

## MVVM module layout

The MVVM pane is composed of one root VM (`ChatPaneViewModel`) and several
sub-VMs that own their own slices of state. Each sub-VM has its view
co-located in the same file as the VM (mirroring this directory's
convention — see `command_palette.py`, `agent_message.py`, `chat_input.py`).

- **view_model.py** — `ChatPaneViewModel`: orchestrates feed (`list[FeedItem]`,
  where each `FeedItem` wraps a `FeedEntry` with a stable monotonic id),
  session mode, active topic, command registry, the agent run lifecycle
  (`start_agent_run` / `_run_agent_turn`), interrupt presentation
  (`present_interrupt`), and the chat-vs-slash dispatch decision. Owns
  the shared `CommandPaletteViewModel` and the `ChatInputViewModel`,
  subscribing to `chat_input.submitted` to route submissions. Does **not**
  own input buffer, enabled, hint, or history — those live on
  `chat_input` (see below). Feed mutations are addressed by id: callbacks
  `feed_append(item_id)`, `feed_remove(item_id)`, `feed_clear()` so consumers
  never need to reason about positional indices.
- **view.py** — `ChatPaneMVVM`: composes `VerticalScroll` (feed),
  `ChatInputView` (bound to `vm.chat_input`), and `CommandPalette` (bound
  to the shared `vm.command_palette`). Subscribes to `vm.feed_append` /
  `vm.feed_remove` / `vm.feed_clear` to mount/dismount per-entry widgets,
  keyed by `FeedItem.id`; no input-area keystroke handling lives here.
- **chat_input.py** — `ChatInputViewModel` + `ChatInputView`. The VM owns
  `buffer`, `enabled`, `hint`, and per-session history (`_history`,
  `_history_index`, `_draft`), plus a reference to the shared palette VM.
  All keystroke semantics that used to round-trip through the pane
  (Enter, Tab, Up/Down, Esc, Ctrl+Enter) are handled by the view directly
  via VM method calls; submissions surface to the pane via the
  `SUBMITTED` callback group. The view subclasses `TextArea` and wires
  the `dirty` subscription manually (rather than extending `ViewBase`)
  to preserve the editing surface.
- **command_palette.py** — `CommandPaletteViewModel` + `CommandPalette`.
  Owns the visible command list, filter text, visibility, cursor. The
  pane constructs the palette VM and hands it to the input VM; the input
  view drives navigation/confirmation through the input VM (which
  delegates), so the palette view only renders state. Exposes
  `has_exact_match(buffer_text)` so the input can decide
  Enter-confirms-palette vs Enter-submits without a widget-tree walk.
- **status_bar.py** — `StatusBarViewModel` + `StatusBarView`. Projection
  of facts that live elsewhere: `mode` and `topic_path` are written by
  the pane VM via `_set_session_mode` / `set_topic` / `clear_topic`;
  `model_name` and `token_usage` are seeded at bootstrap and updated by
  the agent session's `on_token_usage_changed` callback; `verbosity` is
  seeded at bootstrap and updated via an `Options.Agent.AnswerVerbosity`
  subscription. Each setter no-ops on no change and emits the VM's own
  `dirty`, so token-usage chatter during streaming repaints only the
  status bar, not the rest of the pane.
- **shell_command.py** — `ShellCommandViewModel` + `ShellCommandView`.
  Buffer entries that start with `!` are routed by the pane through
  `start_shell_command`, which appends a VM to the feed and schedules
  `vm.execute()` on the worker. The VM owns the `asyncio.subprocess`
  lifecycle, streamed output, return code, and elapsed timing; the view
  subscribes to `dirty` and uses `set_interval` while `vm.running` so
  the elapsed display ticks even when no new output arrives. Input-side
  visual cue (red border) lives on `ChatInputViewModel.shell_mode`,
  which the input view reflects as the `--shell-mode` class.
- **agent_stream_router.py** — `AgentStreamRouter`. Owns the routing of
  agent stream events into feed entries: opens/extends chat segments
  (`AgentMessageViewModel`), opens/extends tool lists
  (`ToolMessageViewModel`), and pins a `ThinkingIndicatorViewModel` to
  the feed tail for the duration of the turn. Exposes
  `start` / `pause` / `close` for turn lifecycle and
  `on_message` / `on_update` callbacks the pane wires into
  `agent_session.stream`. Stateless from the pane's perspective —
  `agent_busy` (worker task aliveness) is the single source of truth
  for "is the agent running". Mutates the feed via
  `pane._append_feed` / `pane._remove_feed`.
- **agent_message.py** — `AgentMessageViewModel` + `AgentMessageView`. A
  single contiguous **chat segment** of agent output (one run of
  streamed text). Multiple of these can appear in one turn, interleaved
  with `ToolMessageViewModel` entries. The view owns its own drain task
  that pulls characters from `vm.body` and writes adaptive-sized slices
  into a `MarkdownStream` so bursty arrivals paint smoothly.
- **tool_message.py** — `ToolMessageViewModel` + `ToolMessageView`. A
  contiguous run of tool calls between agent text segments. Lightweight
  append-only list rendered as a Unicode box-drawing tree. No streaming
  concept — tool calls land atomically.
- **thinking_indicator.py** — `ThinkingIndicatorViewModel` (sentinel) +
  `ThinkingIndicatorView` (braille spinner). Lives in the feed as its
  own entry; the pane mounts it at turn start, repins it to the tail
  whenever a new agent segment is appended, and unmounts it at turn
  close. The VM has no mutable state.
- **chat_message.py** — `ChatMessageView`. Static (non-streaming) view
  for USER / SYSTEM / ERROR messages backed by `ChatMessageData`
  (immutable dataclass — no VM needed). Renders markdown or
  ANSI-via-`rich.Text` based on the `rich` flag.
- **interrupt.py** — `InterruptViewModelBase` + `TestInterruptViewModel` /
  `TestInterruptView`. Future-based interrupt VMs presented inline in
  the feed; pane's `present_interrupt` appends and awaits resolution
  (calls `pause_agent_turn` first to seal the current chat segment but
  keep the thinking indicator mounted, flips input enabled/hint via
  `chat_input.set_enabled` / `set_hint` for the duration).
- **view_model.md** — Design doc for the MVVM rewrite (step-by-step
  rollout notes).

## Legacy

- **_legacy.py** — Original `ChatPane(Widget)`. Re-exported as
  `ChatPane` from `__init__.py` so existing imports keep working until
  the rewrite is the default. Continues to use the top-level
  `widgets/chat_input.py` widget (which has its own internal history and
  walks the widget tree for the command registry) — that widget is
  unchanged because legacy and the commit-instructions input both still
  depend on it.

## Feed ordering rules

Documented inline in `agent_stream_router.py`. Key properties: the
feed is append-only, addressed by `FeedItem.id`, and **position is
not identity** — the router tracks the currently-open chat segment
and tool list by reference, not by being at the tail. Mid-stream user
messages or commands can land between an open segment and its
subsequent chunks. The thinking indicator lives in the feed as its
own entry and gets repinned to the tail (remove + re-append) whenever
a new agent segment is appended.

Agent turn lifecycle: `agent_router.start()` (mounts indicator) →
stream callbacks `agent_router.on_message` / `on_update` route chunks
and tool calls → `agent_router.pause()` (for interrupts, keeps
indicator) or `agent_router.close()` (turn end, removes indicator and
synthesizes a "(no response)" stub if nothing was emitted). The pane
calls these from `_run_agent_turn` and `present_interrupt`; nothing
else touches the routing state.
