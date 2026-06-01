"""``OptionsEditorVM`` — feed-mounted editor for an ``Options`` instance.

Mental model: hold the live ``Options`` target plus a detached ``clone()`` of it as a scratch
buffer. Reads layer staged-on-clone over target. Writes go to the clone for stageable specs or
straight to the target for ``immediate``-flagged ones. ``apply()`` commits the clone's diff
into the target in one shot via ``target.merge_from(clone)``; ``discard()`` rebuilds the clone
from the current target state so the staging buffer reverts.

No lifecycle states. The editor is either mounted (the VM exists) or it isn't — dismissal is
the chat-pane's concern, parallel to the browser widget.

Per-row "this is dirty" rendering signal is the runtime comparison
``clone.get(spec) != target.get(spec)`` — no separate dirty set to keep in sync, and a stage
that lands the user back at the original value correctly de-flags itself.
"""

from __future__ import annotations

from typing import Any

from rhizome.app.options import (
    OptionNamespaceNode,
    OptionScope,
    OptionSpec,
    Options,
)
from rhizome.app.vm import ViewModelBase


class OptionsEditorVM(ViewModelBase):

    def __init__(self, target: Options) -> None:
        super().__init__()
        self.is_navigable = True

        self._target = target
        self._clone = target.clone()

        # Reflect external mutations to ``target`` during editor lifetime — e.g. an agent flips
        # a session option mid-edit, or our own ``set_value`` for an immediate-flagged spec
        # bounces back through this same channel. Without this, un-staged rows would render
        # stale.
        for spec in self.visible_specs:
            target.subscribe(spec, self._on_external_change)

    async def _on_external_change(self, old: Any, new: Any) -> None:
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    @property
    def target(self) -> Options:
        return self._target

    @property
    def scope(self) -> OptionScope:
        return self._target.scope

    @property
    def visible_specs(self) -> list[OptionSpec]:
        """Flat, tree-order list of specs the editor exposes at the current scope."""
        return [s for s in self._target.spec() if s.scope >= self.scope]

    def visible_spec_tree(self) -> tuple[list[OptionSpec], list[OptionNamespaceNode]]:
        """Scope-filtered spec tree for the view's compose loop. Namespaces whose entire
        subtree is filtered out are dropped so the view doesn't emit a bare group title with
        no rows under it.
        """
        top, nodes = self._target.spec_tree()
        return (
            [s for s in top if s.scope >= self.scope],
            [n for n in (self._filter_node(n) for n in nodes) if n is not None],
        )

    def _filter_node(self, node: OptionNamespaceNode) -> OptionNamespaceNode | None:
        visible = [s for s in node.options if s.scope >= self.scope]
        children = [c for c in (self._filter_node(c) for c in node.children) if c is not None]
        if not visible and not children:
            return None
        return OptionNamespaceNode(namespace=node.namespace, options=visible, children=children)

    def get(self, spec: OptionSpec) -> Any:
        """Layered read: clone for stageable specs, target directly for immediate ones."""
        if spec.immediate:
            return self._target.get(spec)
        return self._clone.get(spec)

    def is_dirty_row(self, spec: OptionSpec) -> bool:
        """True when this spec has a pending staged change. Immediate specs are never dirty —
        their writes flow straight to target, so clone and target match by construction."""
        if spec.immediate:
            return False
        return self._clone.get(spec) != self._target.get(spec)

    @property
    def has_staged_changes(self) -> bool:
        return any(self.is_dirty_row(s) for s in self.visible_specs)

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    async def set_value(self, spec: OptionSpec, value: Any) -> None:
        """Validate and stage ``value``. Raises ``ValueError`` on invalid input — the view
        catches this to revert its draft input.

        Immediate-flagged specs skip staging and write straight to target, with an inline
        ``post_update()`` so downstream subscribers (agent rebuilds, etc.) see the change
        right away. Stageable specs go through the clone, whose internal ``Options.__init__``
        auto-cascade subscriptions handle conditional dependents in lockstep.
        """
        if spec.immediate:
            await self._target.set(spec, value)
            await self._target.post_update()
        else:
            await self._clone.set(spec, value)
        self.emit(self.dirty)

    async def apply(self) -> dict[str, tuple[Any, Any]]:
        """Commit staged changes to target in one fell swoop. Returns ``{resolved_name:
        (old, new)}`` suitable for an "Options changed" system message. Clone is left as-is —
        subsequent edits continue from the same buffer, and rows naturally de-flag as
        ``clone.get == target.get`` again."""
        return await self._target.merge_from(self._clone)

    def reset(self) -> None:
        """Drop staged changes by rebuilding the clone from current target state."""
        if not self.has_staged_changes:
            return
        self._clone = self._target.clone()
        self.emit(self.dirty)

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def detach(self) -> None:
        """Tear down target subscriptions. View calls this from ``on_unmount``."""
        for spec in self.visible_specs:
            self._target.unsubscribe(spec, self._on_external_change)
