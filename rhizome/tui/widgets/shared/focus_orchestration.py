"""
Focus orchestration mixin for widgets that coordinate alt+arrow navigation among focusable children.

## Model

Each orchestrator widget owns a small, flat focus graph among its direct focusable children. The graph
is a set of named *nodes* and directional *edges* between them. Node ids correspond 1:1 with widget
ids — a node "details-question" is exactly the widget with `id="details-question"`, reachable via
`query_one("#details-question")`. Subclasses needing a typed reference at a use site should query
again with the specific widget type (e.g., `query_one("#details-question", TextArea)`).

## Recursion via SkipAction + on_focus

The mixin only models one level. Nesting is handled by two existing Textual mechanics:

  - **Upward delegation** — when `focus_neighbour(direction)` returns None, the caller raises
    `SkipAction()` from its action handler, bubbling the unhandled keystroke up to an ancestor whose
    own graph may handle it.
  - **Downward delegation** — when a parent calls `child.focus()`, the child's `on_focus` handler
    (provided by this mixin, opt-out via `AUTO_DELEGATE_FOCUS = False`) re-dispatches focus inward
    to its own graph's `source` node, recursing down nested orchestrators automatically.

Together these mean every orchestrator only reasons about its own immediate children; no widget
ever has to know about another widget's internal structure.

## Inner and outer tiers

The app drives this one mechanism at two keybinding tiers — a UI distinction, not an implementation one
(both are ordinary orchestrators with ordinary graphs):

  - **Inner** (alt+<arrows>) — navigation *within* a self-contained widget: a feed-item proposal, the
    browser, the resource loader.
  - **Outer** (ctrl+<arrows>) — structural navigation *between* the workspace panels and across the chat
    feed. `Workspace` is the outer orchestrator over the docked panels; `ChatArea` owns the outer vertical
    graph across its feed. The two compose through the same fall-through — `ChatArea` binds no
    ctrl+left/right, so those bubble up to `Workspace` to hop panels.

"Inner" and "outer" are relative, not absolute: a feed item is inner to the chat panel, which is inner to
the workspace. It's the same recursion all the way down — the tiers just name which key drives which
structural level.

## Bindings

The mixin does NOT declare bindings. Subclasses declare their arrow bindings (inner or outer, plus any
cursor-navigation bindings) in their own `BINDINGS` list, wire `action_*` handlers to
`self.focus_neighbour(...)`, and decide per-widget what to do with the returned node id (e.g., seed a
cursor on the target). On a None return, raise `SkipAction()` to bubble.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Literal

from textual.css.query import NoMatches
from textual.events import Focus
from textual.widget import Widget


# ========================================================================================================================
# TYPES
# ========================================================================================================================

Direction = Literal["up", "down", "left", "right"]

# A single edge target is either one node id, or an ordered list of fallback ids. When a list is
# given, `_select_neighbour` picks the first available candidate. Bare strings are normalized to a
# one-element list internally — subclasses don't need to wrap singletons.
NodeTarget = str | list[str]


@dataclass(frozen=True)
class FocusGraph:
    """Static description of an orchestrator's focus graph.

    Attributes:
        source: Node id that receives focus when the orchestrator is focused from outside
            (used by `focus_first()` / `on_focus`).
        edges: Adjacency map. `edges[node_id][direction]` gives the target(s) reachable from
            `node_id` in `direction`. Missing entries mean "no edge in that direction" — the
            mixin returns None for those, prompting an upward bubble.
    """

    source: str
    edges: dict[str, dict[Direction, NodeTarget]] = field(default_factory=dict)


# ========================================================================================================================
# MIXIN
# ========================================================================================================================

class FocusOrchestrationMixin(Widget):
    """Mixin providing graph-based alt+arrow focus orchestration for a Textual widget.

    Inherits from `Widget` so the mixin's implementation has proper access to `screen`,
    `query_one`, `parent`, `id`, etc. with full typing.

    Inheritance order — list the mixin LAST in the bases (e.g.,
    `class FlashcardProposal(NavigableFeedItemViewBase, FocusOrchestrationMixin):`). Textual's
    CSS aggregation (`DOMNode._css_bases`) walks only the FIRST DOMNode base at each step and
    skips siblings, so listing the mixin first causes any other base's `DEFAULT_CSS` to be
    silently dropped from the cascade. `on_focus` dispatch is unaffected by ordering — Textual's
    `MessagePump._get_dispatch_methods` iterates the full MRO and invokes every `on_focus` it
    finds — so the sequence trade-off (mixin's auto-delegation firing before or after a
    subclass-defined `on_focus`) doesn't outweigh the CSS concern.

    Subclass contract — at minimum:
      - Set `FOCUS_GRAPH` (class attribute) OR override `_get_focus_graph()` for dynamic graphs.
      - Declare alt+arrow bindings on the subclass and route `action_*` handlers to
        `self.focus_neighbour(direction)`. Raise `SkipAction` when it returns None.

    Optional overrides (in rough order of how often they're touched):
      - `_is_node_available(node_id)` — gate nodes by widget/VM state.
      - `_select_neighbour(source, candidates, direction)` — custom fallback selection.
      - `focus_first()` — dynamic entry-point logic (default focuses `FOCUS_GRAPH.source`).
      - `_current_focus_node()` — non-DOM-derived current node (rare).
      - `_resolve_node(node_id)` — non-id-based widget lookup (rare).

    Opt out of auto downward-delegation by setting `AUTO_DELEGATE_FOCUS = False` (e.g., when the
    host widget needs custom on_focus behavior).

    Note: subclasses using `AUTO_DELEGATE_FOCUS = True` must be focusable themselves
    (`can_focus = True`) so Textual fires `on_focus` and the pass-through can run.
    """

    FOCUS_GRAPH: ClassVar[FocusGraph | None] = None
    AUTO_DELEGATE_FOCUS: ClassVar[bool] = True

    # Orchestrator widgets need to be focusable themselves so external ``widget.focus()`` calls
    # (e.g., chat-pane feed navigation via ``vm.request_focus()``) fire ``on_focus``, which the
    # mixin auto-delegates inward to ``focus_first``. Set as a default on the mixin so it carries
    # through when the *other* base in MRO is itself focusable. Note: a base like
    # ``textual.containers.Vertical`` has ``can_focus = False`` explicitly written into its own
    # ``__dict__`` by Textual's ``Widget.__init_subclass__`` — and since the mixin is listed last
    # in bases (required for CSS aggregation), Vertical's explicit False wins MRO over this. In
    # that case the concrete subclass must set ``can_focus = True`` itself.
    can_focus = True

    # ====================================================================================================
    # PUBLIC API
    # ====================================================================================================

    def focus_neighbour(self, direction: Direction, graph: FocusGraph | None = None) -> str | None:
        """Move focus to the neighbour of the currently-focused node in `direction`.

        By default the move is computed against `self._get_focus_graph()`. Pass `graph` to traverse a
        *different* graph over the same widgets instead — e.g. a narrower overlay graph dispatched from a
        separate key, while the default key keeps driving the main graph.

        Returns the node id that received focus, or None if no move happened — i.e., nothing was
        focused, no edge exists in that direction, or no candidate was available/resolvable.

        Callers (action handlers) should raise `SkipAction()` on a None return so the keystroke
        bubbles to an ancestor orchestrator.

        On a non-None return, callers can use the returned id to apply widget-specific follow-up
        (cursor seeding, etc.) without the mixin having to know about it.
        """
        graph = graph if graph is not None else self._get_focus_graph()
        current = self._current_focus_node(graph)
        if current is None:
            return None

        targets = graph.edges.get(current, {}).get(direction)
        if targets is None:
            return None

        # Normalize bare-string targets to a one-element list so the selection/availability path
        # is uniform regardless of whether the edge declared a singleton or a fallback list.
        candidates = [targets] if isinstance(targets, str) else list(targets)
        available = [c for c in candidates if self._is_node_available(c)]
        if not available:
            return None

        chosen = self._select_neighbour(current, available, direction)
        if chosen is None:
            return None

        widget = self._resolve_node(chosen)
        if widget is None:
            return None

        widget.focus()
        return chosen

    def focus_first(self) -> str | None:
        """Focus the entry-point node of this widget's graph.

        Default focuses `self._get_focus_graph().source`. Returns the node id focused, or None if
        the source node couldn't be resolved.

        Override to compute the entry-point dynamically (e.g., restore last-focused node, or pick
        based on widget state).
        """
        graph = self._get_focus_graph()
        widget = self._resolve_node(graph.source)
        if widget is None:
            return None

        widget.focus()
        return graph.source

    def on_focus(self, event: Focus) -> None:
        """Auto-delegate incoming focus to `focus_first()` so this orchestrator becomes a
        pass-through to its natural entry point.

        Skipped entirely when `AUTO_DELEGATE_FOCUS = False`. The Textual event-dispatch MRO
        ensures any `on_focus` defined on a subclass also runs — no `super()` plumbing needed.
        """
        if not self.AUTO_DELEGATE_FOCUS:
            return

        self.focus_first()

    # ====================================================================================================
    # OVERRIDE SEAMS
    # ====================================================================================================

    def _get_focus_graph(self) -> FocusGraph:
        """Return the focus graph for this widget.

        Default returns `self.FOCUS_GRAPH` (the class attribute). Override for graphs whose shape
        depends on runtime state — e.g., a dialog node only being present when a dialog is mounted,
        or details-pane nodes appearing only when a list selection exists.

        Called fresh on every `focus_neighbour` invocation, so re-evaluation is automatic; do not
        cache aggressively.
        """
        if self.FOCUS_GRAPH is None:
            raise RuntimeError(
                f"{type(self).__name__} uses FocusOrchestrationMixin but neither sets FOCUS_GRAPH "
                f"nor overrides _get_focus_graph()"
            )
        return self.FOCUS_GRAPH

    def _is_node_available(self, node_id: str) -> bool:
        """Return whether `node_id` is currently a valid focus target.

        Default returns True. Override to gate nodes by widget or VM state (e.g., "edit
        instructions only available when `vm.edit_instructions_visible`", "details fields only
        available when not collapsed").

        Called once per candidate during edge traversal — keep it cheap.
        """
        return True

    def _select_neighbour(
        self,
        source: str,
        candidates: list[str],
        direction: Direction,
    ) -> str | None:
        """Pick a target node id from an ordered list of *available* candidates.

        Candidates are pre-filtered by `_is_node_available` — they're guaranteed valid. Default
        returns `candidates[0]` if any, else None.

        Override to add sticky-state behavior ("when re-entering the list, prefer the column you
        came from") or other context-sensitive selection.
        """
        return candidates[0] if candidates else None

    def _current_focus_node(self, graph: FocusGraph | None = None) -> str | None:
        """Determine the node id that currently owns focus.

        Default walks up from `self.screen.focused`, returning the id of the first ancestor whose
        `id` is a key in the current graph. Returns None if nothing is focused or no ancestor is
        in the graph. Resolves against `self._get_focus_graph()` by default; pass `graph` to resolve
        against the same explicit graph `focus_neighbour` is traversing.

        Override only when "current node" isn't derivable from DOM ancestry — rare.
        """
        focused = self.screen.focused
        if focused is None:
            return None

        graph = graph if graph is not None else self._get_focus_graph()
        node_ids = set(graph.edges.keys()) | {graph.source}

        # Walk up from the focused widget, stopping at `self`. This boundary matters for nested
        # orchestrators: a child orchestrator must only resolve focus against its own graph, not
        # against an ancestor's nodes that happen to share an id with one of ours.
        current: Widget | None = focused
        while current is not None and current is not self:
            if current.id in node_ids:
                return current.id
            current = current.parent

        return None

    def _resolve_node(self, node_id: str) -> Widget | None:
        """Resolve a graph node id to its Textual widget.

        Default does `self.query_one(f"#{node_id}", Widget)`, returning None on NoMatches. The
        node-id-≡-widget-id convention should hold for nearly all cases; override only for exotic
        lookups (e.g., resolving to one of N peer widgets based on runtime state).
        """
        try:
            return self.query_one(f"#{node_id}", Widget)
        except NoMatches:
            return None
