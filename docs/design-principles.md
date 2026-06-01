

# MVVM

- High-level design principle:
    - View-model: houses **business logic**, view-agnostic state and behaviour - sometimes accompanied with a data-model as well (lightweight struct of the core data represented by a widget)
    - View: houses the **UI logic** - how is the information in the view-model displayed to the user, how does the user interact with the API, etc.

- In certain cases, the view is practically a _dumb mirror_ of the view-model, which holds no state and merely reflects the view model.

- Common View concerns:
    - The exact arrangement/presentation of widgets that interface with the view model
        - For example, the VM may expose a "set_attribute" API - it is of NO CONCERN to the VM then how the View decides to expose this in the UI. It could be a multiple choice widget, a text input widget, etc., the View is responsible for the exact arrangement, and then coordinating user input to the VM.
    - Focus
        - As we will mention below, focus is typically NOT a VM concern
        - That being said, our ViewModelBase class does have a `request_focus()` callback built into it, which will notify it's corresponding view
        - VMs requesting focus should be used _sparingly_
    - Navigation between widgets
    - Cursors for subwidgets

- Not all Views need VMs:
    - For instance, it is common for a VM to have a main "orchestrator view" and several other "auxiliary views" that reference that same VM
    - These other views can subscribe to the VM's "dirty" callback, to refresh in real-time to updates to the VM.
    - **IMPORANT:** If multiple views subscribe to the same VM, ensure that only ONE view actually calls `ViewBase.__init__` with the given VM instance. Otherwise the `request_focus()` may dispatch to the wrong parent view.

- Typically the direction of communication is FROM view TO view-model
    - Keystrokes and other forms of user input are caught by the view, and used to update the view model
- Communication FROM view-model TO view is kept to a minimum, and handled through callbacks:
    - Core callback: self.dirty
        - All VMs have this callback
        - All views subscribe their "_refresh" method to this automatically upon construction
        - VMs use this callback to indicate that "something in the display state may need to be refreshed"
    - Callbacks should be kept to an _absolute minimum_, to prevent too much view/view-model coupling.

- Handling parent/child relationships with view/view-model separation
    - View-model creates child view-models - refreshes the view
    - View inspects the child view models that may have been created since last refresh - spawns views linked to the corresponding view models
    - No need for a fully generalized process - parent view knows exactly which child views it needs to manage

- Textual specifics of the View:
    - Still utilizes a compose to do the initial widget mounting
    - Mostly modifies reactive attributes instead of mounting widgets - allows textual to determine when a region needs to be redrawn

- Focus, and what that means for priority keybindings
    - For the most part, VMs do NOT need to know that they are the "focused" one, that's a view-side concern
    - Focus determines the _routing of keystrokes and other input_, and the hierarchy of widgets responsible for handling events
        - The presently focused widget is the first responder to input, then it's parent, and so on, letting the message bubble up naturally

    - Focus is a view-side concern - the arrangement
    - VMs that manage multiple child VMs can, and should, post a sort of weak "_focus(widget: str)" message when requesting to the view to refocus, in order to keep view and view-model in sync.


- Code smells:
    - Too many small callback groups
        - if you need specific callback groups for specific parts of the UI, especially if this is needed to avoid infinite loops between view/VM, then something is wrong.

    - Using callbacks to communicate VM -> View, but then modifying the VM from within the View's handler.
        - Callbacks are how the VM notifies the View of certain events
        - Whatever the View does to handle this, it typically should NOT be _mutating the VM state_ within the handler
        - This is because VM state mutations, by default, emit their own

    - VM state mutators that don't emit(self.dirty)
        - This is typically a big error in judgment: the VM is the ground truth for "what the view should look like", so anything that modifies the VM state should invariably emit a self.dirty call.
        - Without a self.dirty call, the View and VM become out of sync until the next thing that emits self.dirty

        - In some circumstances, there _is_ room for an infinite loop, for instance:
            - View responds to an event type T by calling VM's API
            - VM emits self.dirty
            - View._refresh repaints an inner widget
            - The repaint emits the same event type T through textual
            - Infinite loop

        - To guard against this, a good practice is to ensure that VMs only emit self.dirty when the state has _actually changed_
            - The above example comes from a case of a VM holding the state of a DataTable view, and responding to DataTable.RowHighlighted events in order to set the VM cursor, but setting the VM cursor would repaint the table, which would cause textual to emit a new RowHighlighted event, etc.
            - To fix this, we guard against emit(self.dirty) in VM.set_cursor(new_cursor) by returning early when self.cursor == new_cursor - no data changed internally, so we don't need to repaint.
            - The second round trip of the event isn't really avoidable thanks to how DataTable repainting works in textual, and we probably could've avoided this by using something other than DataTable.move_cursor in View._refresh, but the guard in the VM also works.

        - **Caveat:** the identity guard is necessary but not sufficient when the View ALSO pushes back to the framework during _refresh (the DataTable.move_cursor workaround above). Under fast event rates (e.g. holding down on a big table) the framework's event queue desynchronizes: by the time we process T(N+1), the framework has already advanced to N+2 with T(N+2) queued behind us. _refresh's "sync" call then both snaps the framework backwards AND posts a fresh T(N+1) behind T(N+2), and the two interleave indefinitely — every step is a genuinely new cursor value, so the identity guard never fires.
            - Rule of thumb: when the View is forwarding an event from the framework, the framework is the source of truth for that event — don't push back through the same channel while the handler is on the stack. Scope the framework-sync in _refresh (the move_cursor call, etc.) to VM-initiated cursor changes only. A _handling_X flag set across the event handler is the simplest gate.




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
