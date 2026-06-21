"""ViewModelBase — the MVVM view-model base class.

The callback/emitter machinery itself (groups, weakref subscriptions, ``emit_once`` batching) lives
in ``rhizome.utils.callbacks``; this module layers the view-model conventions on top: the standard
``OnDirty`` / ``RequestFocus`` / ``OnHint`` channels and the focus protocol. ``CallbackGroup`` and
``Emitter`` are re-exported here for convenience, since view code constructs annotations and emitter
parameters against them.
"""

from __future__ import annotations

from rhizome.utils.callbacks import CallbackGroup, CallbackHost, Emitter

__all__ = ["CallbackGroup", "CallbackHost", "Emitter", "ViewModelBase"]


class ViewModelBase(CallbackHost):
    """Base class for MVVM view-models.

    Communication model
    -------------------
    ``CallbackGroup``s (``OnDirty``, ``RequestFocus``, plus any subclass-defined groups) are
    **exclusively** a VM → View communication channel. Every other direction uses direct method
    calls:

    - View → VM: direct method call. The notable view-driven entry points are ``notify_focused()`` and
      ``notify_blurred()``, which views must call from their Textual ``on_focus`` / ``on_blur`` handlers.
    - VM → VM: direct method call on the other VM. If a parent VM needs a sibling to update, it calls
      a public method on that sibling; the sibling emits *its own* events toward the view.
    - VM → View: the view subscribes a callback to a VM's CallbackGroup; the VM emits, the view reacts.

    This is why there is no ``OnBlurred`` callback — the VM has no one to broadcast "I was blurred"
    to. Blur is always view-initiated; the view sees the Textual event and calls ``notify_blurred()``
    to give the VM a chance to react locally. There is no ``request_blur`` either: if one VM wants
    another unfocused, it requests focus elsewhere.

    Standard callbacks
    ------------------
    - ``Callbacks.OnDirty`` — "something changed; please repaint." Fired by mutators on this VM;
      subscribers are typically view-side ``_refresh`` methods.
    - ``Callbacks.RequestFocus`` — "please give this VM (its widget) focus." Fired by
      ``request_focus()``; the canonical subscriber is the view's ``Widget.focus()``.
    - ``Callbacks.OnHint`` — "here's a short advisory message; display it if you like." Fired by
      ``hint(msg)``. No subscriber is required; views are free to ignore hints entirely.

    Naming convention
    -----------------
    - ``On<Event>`` for after-the-fact observations (``OnDirty``, ``OnSelectionChanged``).
    - ``Request<Action>`` for imperative VM → View directives (``RequestFocus``).
    - ``notify_<event>`` / ``request_<action>`` as method names for the corresponding inbound
      (View → VM) and outbound (VM → View) entry points.

    Subclasses extend ``Callbacks`` via standard class inheritance and register their groups in
    one shot through ``make_callback_groups`` — the dict-spec values document the payload shape:

        class TopicTreeModel(ViewModelBase):
            class Callbacks(ViewModelBase.Callbacks):
                OnSelectionChanged = "OnSelectionChanged"
                OnTopicDeleted     = "OnTopicDeleted"

            def __init__(self):
                super().__init__()
                self.make_callback_groups({
                    self.Callbacks.OnSelectionChanged: None,       # nullary
                    self.Callbacks.OnTopicDeleted:     int,        # one int arg
                })

    Subscription lifetime
    ---------------------
    Subscribers are weakly held (see ``rhizome.utils.callbacks``). ``ViewBase.on_unmount``
    unsubscribes explicitly for eager teardown — preventing a callback fire between unmount and GC —
    not for leak prevention.

    Emitters are single-VM
    ----------------------
    The ``Emitter`` yielded by ``emit_once`` is tied to the VM that created it. Passing a CallbackGroup
    owned by another VM raises ``ValueError``. Cross-VM batching is not a pattern in this codebase —
    each VM emits its own events through its own methods, and any apparent "atomicity" between sibling
    VM updates is provided by the framework's render-frame coalescing, not by our emit machinery.

    Emitter threading convention
    ----------------------------
    Methods on a VM that may participate in *that same VM's* ``emit_once`` batch take an
    ``emitter: Emitter | None = None`` parameter; ``None`` means "fire directly via ``self``" (since
    ``CallbackHost.emit`` is signature-compatible with ``Emitter.emit``).

    Notably, ``notify_focused`` and ``notify_blurred`` do **not** take an emitter. They are only ever
    called from a view's Textual event handler (which is outside any emit_once chain) — there is no
    legitimate VM caller, so threading an emitter would be meaningless.

    Async boundary
    --------------
    Emitters batch a single *synchronous* chain of execution and never cross task spawns.
    ``asyncio.create_task(...)`` is the stopping point — a spawned coroutine starts a fresh emit
    context and opens its own ``emit_once`` if it needs to coalesce. Async callbacks fired from timers
    should call ``self.emit(...)`` directly, never thread a captured emitter across the boundary.
    """

    class Callbacks:
        """Callback-group keys for this VM and its subclasses. A plain class (not ``Enum``) so
        subclasses can extend the namespace via standard inheritance — ``self.Callbacks.OnDirty``
        resolves on every subclass, alongside whatever each subclass adds:

            class TopicTreeModel(ViewModelBase):
                class Callbacks(ViewModelBase.Callbacks):
                    OnSelectionChanged = "OnSelectionChanged"
                    ...

        Values are strings matching their attribute name. Equality and hashing are string-based,
        so different VM classes that happen to use the same key string collide cleanly.
        """
        OnDirty = "OnDirty"
        RequestFocus = "RequestFocus"
        OnHint = "OnHint"

    # Whether this VM is part of the chat pane's ctrl+up/ctrl+down navigation rotation. Default False;
    # feed VMs that present an interactive surface (interrupts, branch indicators) flip this to True
    # in their __init__, and back to False when they become non-interactive (e.g. on interrupt resolve).
    # TODO: this lives on ``ViewModelBase`` for now because adding a feed-only base class would touch
    # every feed VM at once. Consider splitting ``ViewModelBase`` → ``FeedEntryViewModel`` (houses
    # ``is_navigable``) → concrete VMs once the chat-pane MVVM port stabilizes.
    is_navigable: bool = False


    def __init__(self):
        super().__init__()

        self.make_callback_groups({
            ViewModelBase.Callbacks.OnDirty:      None,
            ViewModelBase.Callbacks.RequestFocus: None,
            ViewModelBase.Callbacks.OnHint:       str,
        })


    def request_focus(self, emitter: Emitter | None = None) -> None:
        """Request that this VM (and its view) take focus. Emits on ``Callbacks.RequestFocus``; the
        canonical subscriber is the view's ``Widget.focus()``, which causes Textual to focus the
        widget and eventually fire the view's ``on_focus`` → ``VM.notify_focused()``.

        Callable from VM code — typically a parent VM orchestrating children, or any code path that
        wants to direct focus programmatically. Accepts an optional ``emitter`` so the focus emit can
        participate in a caller's ``emit_once`` batch on this same VM (emitters are single-VM scoped).

        Does NOT emit ``OnDirty`` here: the downstream ``Widget.focus() → on_focus → notify_focused``
        chain emits it for us. If the widget is already focused, ``Widget.focus()`` is a no-op and
        ``notify_focused`` isn't called — which is correct, because nothing changed.
        """
        if emitter is None:
            emitter = self
        emitter.emit(self.Callbacks.RequestFocus)


    def hint(self, msg: str, emitter: Emitter | None = None) -> None:
        """Surface a short advisory message for the view to optionally display via ``OnHint``.

        No subscriber is required — views are free to ignore hints entirely (or display them in a
        transient status slot, badge, etc.). Accepts an optional ``emitter`` so the emit can
        participate in a caller's ``emit_once`` batch on this same VM.
        """
        if emitter is None:
            emitter = self
        emitter.emit(self.Callbacks.OnHint, msg)


    def notify_focused(self) -> None:
        """View-side notification that this VM's view has received focus.

        Inbound counterpart to ``request_focus``. **Must only be called from the view's Textual
        ``on_focus`` event handler** — never from VM code. Focus events arrive outside any
        ``emit_once`` chain, so this method does not accept an ``emitter`` parameter.

        You MUST NOT call ``self.request_focus()`` from within ``self.notify_focused()`` — that creates
        an infinite loop:

            self.request_focus() -> view.focus() -> view.on_focus()
                                 -> self.notify_focused() -> self.request_focus() -> ...

        Calling a *child* VM's ``request_focus()`` is fine — that is the whole point of the delegation
        pattern. A ParentVM that orchestrates children can, on regaining focus, forward focus to the
        appropriate child:

            ParentView.on_focus()
                -> ParentVM.notify_focused()
                -> ParentVM decides which child should be focused
                -> ChildVM.request_focus()
                -> ChildView.focus()
                (-> ChildView.on_focus() -> ChildVM.notify_focused() -> ...)

        Default impl emits ``Callbacks.OnDirty``. Most VMs need a repaint on focus change (focused-
        region styling, hint changes, etc.); Textual handles purely-CSS focus styling automatically,
        but content changes require a refresh. Override and skip the OnDirty emit if your VM truly
        has no focus-dependent rendering.
        """
        self.emit(self.Callbacks.OnDirty)


    def notify_blurred(self) -> None:
        """View-side notification that this VM's view has lost focus.

        Symmetric to ``notify_focused``: called only from the view's Textual ``on_blur`` handler, never
        from VM code. There is no ``request_blur`` (and no ``OnBlurred`` callback) because blur is
        always view-initiated — if some VM wants this one unfocused, it requests focus elsewhere.

        Default impl emits ``Callbacks.OnDirty`` for the same reasons as ``notify_focused``. Override
        and skip the emit if your VM has no blur-dependent rendering.
        """
        self.emit(self.Callbacks.OnDirty)
