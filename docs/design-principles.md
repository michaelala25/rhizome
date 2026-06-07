# MVVM

## What lives where

- **View-model (VM):** business logic, view-agnostic state, lifecycle. The single source of truth for "what the user has committed to so far."
- **View:** how the VM is presented (compose, styling, key routing) plus _transient UI-only state_ — buffers, cursors, focus, animations, dialog choice positions, etc.

The split is sharper than "logic vs rendering." It's about _what survives a re-mount_: if you destroyed the view and rebuilt it from the VM, what would you lose? Anything irrecoverable belongs on the VM. Anything that's just "where the caret is right now" belongs on the view.

## Direction of communication

- View → VM: direct method calls. The view catches input, decides what it means, and calls a VM method.
- VM → View: named callback groups (see below). The view subscribes; the VM emits.
- VM → VM: direct method calls. If a parent VM needs siblings to update, it calls _their_ public methods; they each emit their own groups. Emitters don't cross VMs.

`notify_focused()` and `notify_blurred()` on the VM are the _inbound_ counterparts to the focus events — `ViewBase.on_focus` / `on_blur` call them automatically. They're the only place a raw Textual event legitimately translates into a VM method call from outside an action handler.

## Callback groups

Callbacks are the only VM → View channel. The current pattern is: declare one group per _purpose_, named after the event, carrying whatever payload makes the subscriber's job cheap.

```python
class CommitProposalModel(ViewModelBase):
    class Callbacks(ViewModelBase.Callbacks):
        OnEntriesChanged  = "OnEntriesChanged"
        OnRevisingChanged = "OnRevisingChanged"
        OnDone            = "OnDone"

    def __init__(self, ...) -> None:
        super().__init__()
        self.make_callback_groups({
            self.Callbacks.OnEntriesChanged:  list[int],
            self.Callbacks.OnRevisingChanged: bool,
            self.Callbacks.OnDone:            Outcome,
        })
```

- **Naming.** `On<Event>` for after-the-fact observations (`OnEntriesChanged`). `Request<Action>` for VM → View directives (`RequestFocus`). Method names follow `notify_<event>` (inbound) / `request_<action>` (outbound).
- **Payload shape.** Carry what the view needs to respond _granularly_. `OnEntriesChanged: list[int]` lets the view repaint only the touched rows instead of redrawing the whole table. Reach for nullary `None` only when there's genuinely nothing useful to carry.
- **`OnDirty` is the fallback, not the channel.** Every VM still has it (and `ViewBase` auto-subscribes `_refresh` to it), but use it only when nothing more specific applies. If you find yourself emitting `OnDirty` with payload-shaped intent ("this index changed", "this flag flipped"), define a real group for it.

**Earlier guidance reversed.** A previous version of this doc said callbacks should be "kept to an absolute minimum." That was wrong in practice — narrow channels made VMs hard to consume granularly and pushed too much work back through `OnDirty`. The current rule is: as many groups as you need to make subscribers' lives obvious; don't pad, don't ration.

## Where state lives

The recent proposal refactors collapsed a lot of "VM state" back onto the view. The heuristics that fell out:

| Kind of state                                            | Lives on |
|----------------------------------------------------------|----------|
| Committed data (entries, flashcards, ids)                | VM       |
| Lifecycle state (`REVIEWING`, `DONE`, …)                 | VM       |
| Anything another VM or external consumer needs to read   | VM       |
| TextArea / draft / buffer text                           | View     |
| Cursors (DataTable row, ListMenu item, dialog choice)    | View     |
| Collapsed / expanded flags, hover, scroll offset, focus  | View     |
| Dialog UI state (which option is highlighted in a modal) | View     |

Buffers reach the VM only at _confirm points_ — Accept gestures, Submit buttons. Per-keystroke round-trips create coupling that buys nothing; the VM doesn't care about half-typed text. Cursor moves silently discard in-flight buffer edits, view-side.

On the proposal widgets concretely: `CommitProposalModel.entries` is the committed list; the live title/content text is in `ContentEditor` (view); the focused row index is `DataTable.cursor_row` (view); edits reach the VM only when the user accepts the per-entry editor.

## Focus

Focus is a view concern. The VM mostly shouldn't track who's focused, but it does have a thin channel for directing focus:

- `request_focus()` on the VM emits `Callbacks.RequestFocus`; `ViewBase` subscribes `Widget.focus()` to it. Use sparingly — typical case is "the user just transitioned into this state; the natural widget for that state should grab focus" (e.g. revision-instructions on `request_revision`).
- `notify_focused()` / `notify_blurred()` are _inbound_, called from the view's `on_focus` / `on_blur`. Default impl emits `OnDirty` since most VMs need a paint when focus changes (hint text, cursor styling, etc.). Override to skip the emit if your VM truly has no focus-dependent rendering.

Don't put a `focused: bool` on the VM unless a sibling VM or external consumer genuinely needs to read it. Focus routing (priority bindings, the focus chain) is the framework's concern, not the VM's.

## Parent/child views

A VM composing child VMs is fine — but only when the child has _genuine business logic_. Pre-refactor, the proposal widgets had a per-item details VM whose sole job was "hold the current TextArea buffers." That's transient UI state, not business logic, and it now lives in `ContentEditor` view-side.

When child VMs do exist:
- Parent VM creates them; parent View mounts the corresponding child Views.
- No fully-generalised registry — the parent View knows the exact shape of children it expects.
- Multiple Views can subscribe to one VM, but **only one View calls `ViewBase.__init__(vm)`** — otherwise `request_focus()` fires `.focus()` on every subscriber and races.

## State machines for terminal lifecycles

Proposal-style widgets (commit-proposal, flashcard-proposal) share a shape worth naming:

- N _resting_ states the user can mutate from (`REVIEWING`, `REQUESTING_REVISION`).
- One _terminal_ state (`DONE`) no mutator can leave.
- A separate `Outcome` enum describing _how_ it ended (`ACCEPTED`, `REVISED`, `CANCELLED`).
- Mutators assert non-DONE; terminal transitions fire a single `OnDone(outcome)`. Interrupt models subscribe to that to resolve their future.

DONE + Outcome beats overloading the state enum with `DONE_ACCEPTED` / `DONE_CANCELLED` variants — those would carve states along an axis that isn't really a state (see "Cartesian explosion" below). This is _one_ common shape, not the only valid one; use it when terminality matters.

## Textual specifics

- Initial widget mounting goes through `compose`. After mount, prefer mutating reactive attributes / inline styles over mount/unmount cycles — Textual can scope redraws to the affected region.
- Lazy mount/unmount _is_ still the right move for mode-driven subtrees (mutex of dialogs, alternative panes) — see the Performance section.

## Code smells

- **Putting view-only state on the VM.** Cursors, buffers, dialog cursors, collapsed flags. Default to view-side; promote only if business logic genuinely needs it.
- **Mutating the VM from inside a VM → View callback handler.** The handler is meant to _react_ to a VM change; if it then mutates the same VM, you're using callbacks as control flow. The VM should have emitted whatever new event you actually need.
- **VM mutators that don't emit anything.** If state changed, _something_ needs to fire so subscribers stay in sync. Usually a specific group with a useful payload; fall back to `OnDirty` only as a last resort.
- **`OnDirty` with no payload, everywhere.** Often signals "I haven't thought about what changed." Replace with a named group.
- **Identity guards as the primary defence against feedback loops.** The guard itself is fine (don't emit when nothing changed), but if you're reaching for it because the view round-trips its own framework events through the VM, the underlying design has the state living in two places. See below.

## The DataTable cursor-sync trap

There is a feedback-loop pattern worth knowing about:

> A VM holds a `cursor` field mirroring `DataTable.cursor_row`. The view subscribes to `DataTable.RowHighlighted` and writes the new index to the VM. The VM emits, the view's `_refresh` calls `DataTable.move_cursor(...)` to keep the framework in sync — which fires another `RowHighlighted`. Infinite loop.

Accepted workaround:
- **Identity guard in the VM mutator:** don't emit when the value didn't actually change.
- **`_handling_<event>` flag in the view,** set across the event handler and checked in `_refresh`, so we don't push back into the framework while it's the source of truth for that event.

**The identity guard is necessary but not sufficient** under fast event rates. The framework's queue desynchronises: by the time we process `T(N+1)`, the framework has already advanced to `N+2` with `T(N+2)` queued behind us. `_refresh`'s sync call both snaps the framework backwards _and_ posts a fresh `T(N+1)`; the two interleave indefinitely and every step is a genuinely new cursor value, so the guard never fires.

Rule of thumb: when the View is forwarding a framework event, the _framework_ is the source of truth for that event. Don't push back through the same channel while the handler is on the stack. Scope the framework-sync in `_refresh` to VM-initiated changes only; a `_handling_X` flag is the simplest gate.

**The deeper smell, though, is the duplication.** This whole class of bug only exists because the cursor was living in two places — VM _and_ framework — and we were trying to keep them in sync. The recent proposal refactors removed the cursor from the VM entirely; the DataTable owns it, the view reads `cursor_row` when it needs the current index, and the round-trip vanishes. The workaround above is still load-bearing in older code, but if you're reaching for it on _new_ code, the better question is whether the data needs to be duplicated at all. View ↔ framework round-trips for view-only state shouldn't be flowing through the VM in the first place.




## State Machines

- Intuitively a "state" is used to define a certain, self-contained "epoch" in the lifetime of an object.

- What constitutes a "state" vs an "attribute"?
    - A "state" determines a _slice of the public API that is callable_ - in other words, a state is an _equivalence class_ of methods that can be used to mutate internal representation.
        - For example, `DONE_COLLAPSED` and `DONE_EXPANDED` in the FlashcardReview widget would have the exact same set of callable methods, and we can transition to and from the two states at any point, so instead we should represent this as a single `DONE` state with a `collapsed` attribute.

- Cartesian explosion
    - N boolean attributes corresponds to 2^N different states in the state machine to keep track of
    - Thus, attributes should be preferred to states to restrict the size of the state/transition space whenever possible


# Performance

## Stylesheets are the biggest TUI bottleneck

- Textual's CSS engine is the most common source of perceptible lag in interactive UIs (focus shifts, navigation, dialog open/close). The pyinstrument flame graph almost always shows `StyleSheet.apply`, `_process_component_classes`, and `replace_rules` at the top of the active time.

- Cost model:
    - **`apply`** — runs the full selector engine for a node, matching every rule against the node's path, then merging matching styles. Called once per "node needs restyle" event.
    - **`_process_component_classes`** (PCC) — for each component class on the node, creates a virtual child node and calls `apply` on it. Widgets like `DataTable`, `Tree`, `Input`, and `TextArea` carry many component classes — each PCC call triggers an inner cascade of `apply` calls.
    - **`replace_rules`** — final rules-map swap + diff on the node.
    - **`_check_rule`** — the selector-matching primitive. Hot but individually cheap; rarely the bottleneck.

- A single focus shift in a complex tree can trigger 100+ `apply` calls. The amplifier is usually a single pseudo-selector or ancestor class-change that causes Textual to defensively reapply styles to an entire subtree.


## Avoid `:focus-within` and ancestor-class toggles

- The biggest stylesheet smell in Textual is using `:focus-within` on a node with many descendants. When focus shifts anywhere inside the subtree, Textual reapplies styles to every descendant of that node — even if no descendant rule actually keys on the focus state. The engine doesn't analyze descendant selectors; it just walks the subtree defensively.

- The same trap applies to `widget.set_class("-foo")` on an ancestor. The class change triggers a descendant cascade for the same reason — Textual can't tell whether any descendant rule keys on `-foo` without re-running the matcher, so it doesn't try.

- **The remedy: inline styles for ancestor-keyed visual state.** Inline styles (e.g. `widget.styles.border = ("solid", "#6a6a6a")`) are *node-scoped*. They can't be selectors, so the engine doesn't have to revisit descendants when they change. Track the state in Python — `on_descendant_focus` / `on_descendant_blur` for focus-within, `on_enter` / `on_leave` for hover, etc. — and assign the inline style directly.

- `widget.styles.border = None` clears the inline override and lets CSS rules take over again — so you can mix CSS for default/hover with inline for the cascade-triggering state.

- **What's safe:** pseudo-selectors that only match the node itself (`:focus`, `:hover`, `:disabled`) don't cascade. The cascade fires when an *ancestor's* state changes, not the matched node's own state.


## Prefer lazy mounting over CSS-driven visibility

- A widget that's `display: none` still pays the full `apply` / `_process_component_classes` cost on every style invalidation in its subtree. Hidden-but-mounted ≠ free.

- For mode-driven or rarely-shown subtrees (mutex of dialogs, alternative panes that swap on a state flip, etc.), mount only the active widget and unmount on transition. The pattern is small: a `_make_<thing>` factory, a `_mount_<thing>` / `_unmount_<thing>` pair, and a sync method that's called when the controlling state changes.

- Trade-off: each mount re-runs `compose` and `on_mount`, including any VM subscription wiring. For mode switches that fire on explicit user action (`tab` key, dialog open) the cost is dwarfed by the avoided CSS work; for state that flips on every refresh, lazy-mount is the wrong move.


## The named-handler MRO gotcha

- Textual auto-dispatches named `on_<event>` handlers (`on_focus`, `on_blur`, `on_mount`, etc.) at *every level of the MRO*. A subclass's `on_focus` does **not** replace the base class's — both fire on every event, automatically.

- **Don't call `super().on_focus(event)` in a named handler.** The base's handler is already being called by the dispatcher; the explicit `super()` makes it fire a second time. Subclass-specific behavior just goes in the subclass's named handler — no `super()` glue needed.

- Same dynamic with the `@on(EventType)` decorator form: Textual collects all decorated handlers in the MRO chain. Decorating in both the base and subclass causes both to fire without any explicit `super()` call.


## Knowing when you're hitting it

- The pyinstrument profiler (`ctrl+f12` in the app) gives stack-level visibility. `StyleSheet.apply` near the top of the flame graph is the smoke signal.

- For per-widget granularity (pyinstrument is sampling-based, so it never captures function args), `rhizome/tui/_profiling.py` monkey-patches the four core `Stylesheet` methods and produces a text report keyed by `(widget_class, widget_id)`. It runs alongside the pyinstrument session — same `ctrl+f12` toggle, the report lands next to the HTML in `/tmp/rhizome-profiles`.

- A healthy focus-shift cost is ~20 `apply` calls, dominated by the widget losing focus, the widget gaining focus, and their scrollbars. If a single shift is producing 100+ `apply` calls, something is firing a subtree cascade — look for `:focus-within`, ancestor class toggles, or any pseudo-selector matching at high frequency on a heavy ancestor.
