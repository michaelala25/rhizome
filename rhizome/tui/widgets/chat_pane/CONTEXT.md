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

- **view_model.py** — `ChatPaneViewModel`: orchestrates feed (`list[FeedEntry]`),
  session mode, active topic, command registry, the agent run lifecycle
  (`start_agent_run` / `_run_agent_turn`), interrupt presentation
  (`present_interrupt`), and the chat-vs-slash dispatch decision. Owns
  the shared `CommandPaletteViewModel` and the `ChatInputViewModel`,
  subscribing to `chat_input.submitted` to route submissions. Does **not**
  own input buffer, enabled, hint, or history — those live on
  `chat_input` (see below).
- **view.py** — `ChatPaneMVVM`: composes `VerticalScroll` (feed),
  `ChatInputView` (bound to `vm.chat_input`), and `CommandPalette` (bound
  to the shared `vm.command_palette`). Subscribes to `vm.feed_append` /
  `vm.feed_clear` to mount/remove per-entry widgets; no input-area
  keystroke handling lives here.
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
- **shell_command.py** — `ShellCommandViewModel` + `ShellCommandView`.
  Buffer entries that start with `!` are routed by the pane through
  `start_shell_command`, which appends a VM to the feed and schedules
  `vm.execute()` on the worker. The VM owns the `asyncio.subprocess`
  lifecycle, streamed output, return code, and elapsed timing; the view
  subscribes to `dirty` and uses `set_interval` while `vm.running` so
  the elapsed display ticks even when no new output arrives. Input-side
  visual cue (red border) lives on `ChatInputViewModel.shell_mode`,
  which the input view reflects as the `--shell-mode` class.
- **agent_message.py** — `AgentMessageViewModel` + `AgentMessageView`. A
  single contiguous agent turn (interleaved text + tool-call segments).
  Mounted into the feed by the pane's peek-tail routing; the view
  diff-renders against its last paint on each `dirty`.
- **interrupt.py** — `InterruptViewModelBase` + `TestInterruptViewModel` /
  `TestInterruptView`. Future-based interrupt VMs presented inline in
  the feed; pane's `present_interrupt` appends and awaits resolution
  (closes any open agent turn first, flips input enabled/hint via
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

Documented inline in `view_model.py` above `open_agent_turn`. Key
property: the feed is append-only and **position is not identity** — the
currently open agent message VM is tracked by reference
(`_current_agent_message`), not by being at the tail. Mid-stream user
messages or commands can land between an open agent VM and its
subsequent chunks.
