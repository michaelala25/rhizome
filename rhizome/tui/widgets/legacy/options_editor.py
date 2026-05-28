"""Inline TUI widget for editing options."""

from __future__ import annotations

import re
from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widget import Widget

from textual.widgets import Button, Input, Label, Rule, Select, Static

from .navigable import WidgetDeactivated
from rhizome.tui.options import (
    ChoicesOptionSpec,
    ConditionalChoicesOptionSpec,
    ConditionalIntRangeOptionSpec,
    IntRangeOptionSpec,
    OptionNamespaceNode,
    OptionScope,
    OptionSpec,
    Options,
    ToggleOptionSpec,
)

# ---------------------------------------------------------------------------
# Widget builders: OptionSpec type → widget factory
# ---------------------------------------------------------------------------

WIDGET_BUILDERS: dict[type[OptionSpec], Any] = {
    ChoicesOptionSpec: lambda spec, val, wid, **kw: Select(
        [(str(c), c) for c in spec.choices], value=val, id=wid
    ),
    ConditionalChoicesOptionSpec: lambda spec, val, wid, **kw: Select(
        [(str(c), c) for c in spec.choices_for(kw["condition_value"])],
        value=val,
        id=wid,
    ),
    IntRangeOptionSpec: lambda spec, val, wid, **kw: Input(
        str(val), placeholder=f"{spec.min}-{spec.max}", id=wid, type="integer"
    ),
    ConditionalIntRangeOptionSpec: lambda spec, val, wid, **kw: Input(
        str(val),
        placeholder="{}-{}".format(*spec.range_for(kw["condition_value"])) if "condition_value" in kw else "",
        id=wid,
        type="integer",
    ),
    ToggleOptionSpec: lambda spec, val, wid, **kw: Select(
        [(str(c), c) for c in spec.choices], value=val, id=wid
    ),
}


def _sanitize_id(resolved_name: str) -> str:
    """Turn a dotted resolved name into a valid Textual widget ID.

    Widget IDs may only contain letters, numbers, underscores, or hyphens
    and must not begin with a number.
    """
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "-", resolved_name)
    if sanitized and sanitized[0].isdigit():
        sanitized = f"_{sanitized}"
    return f"opt-{sanitized}"


def _build_widget(
    spec: OptionSpec, value: Any, widget_id: str, options: Options | None = None
) -> Widget:
    builder = WIDGET_BUILDERS.get(type(spec))
    if builder is not None:
        kwargs: dict[str, Any] = {}
        if isinstance(spec, (ConditionalChoicesOptionSpec, ConditionalIntRangeOptionSpec)) and options is not None:
            kwargs["condition_value"] = options.get(spec.condition)
        return builder(spec, value, widget_id, **kwargs)
    return Input(str(value), id=widget_id)


class OptionsEditor(Widget):
    """Inline editor for viewing/modifying options."""

    DEFAULT_CSS = """
    OptionsEditor {
        height: auto;
        padding: 0 2 1 2;
        background: rgb(16, 16, 16);
        border: round rgb(86, 126, 160);
    }
    OptionsEditor #options-title {
        text-style: bold;
        margin-top: 1;
        margin-bottom: 1;
    }
    OptionsEditor .option-row {
        height: auto;
        margin-top: 1;
    }
    OptionsEditor .option-row .option-info {
        width: 1fr;
        height: auto;
    }
    OptionsEditor .option-row .option-name {
        height: auto;
    }
    OptionsEditor .option-row .option-desc {
        color: $text-muted 60%;
        width: 1fr;
    }
    OptionsEditor .option-row Select {
        width: 40;
    }
    OptionsEditor .option-row Input {
        width: 40;
    }
    OptionsEditor .option-group-title {
        margin-top: 1;
        color: $text-muted;
        text-style: bold;
    }
    OptionsEditor Rule {
        margin-top: 1;
        margin-bottom: 0;
        color: rgb(50, 50, 50);
    }
    OptionsEditor #options-done {
        margin-top: 1;
        width: auto;
        background: transparent;
        border: round rgb(80, 80, 80);
        color: $text-muted;
        min-width: 10;
        text-align: center;
    }
    OptionsEditor #options-done:hover {
        border: round rgb(120, 120, 120);
        color: $text;
    }
    OptionsEditor #options-dismiss {
        dock: right;
        width: 3;
        min-width: 3;
        height: 1;
        background: transparent;
        border: none;
        color: $text-muted;
        margin: 0;
        padding: 0;
    }
    OptionsEditor #options-dismiss:hover {
        color: $error;
    }
    """

    class Dismissed(Message):
        """Posted when the user clicks the dismiss button (no changes applied)."""

    class Done(Message):
        """Posted when the user clicks Done."""

        def __init__(self, changes: dict[str, tuple[Any, Any]]) -> None:
            super().__init__()
            self.changes = changes
            """Map of resolved_name → (old_value, new_value) for changed options."""

    def __init__(self, options: Options, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._options = options
        # Build a map from widget_id → spec for event handling
        self._widget_specs: dict[str, OptionSpec] = {}
        # Snapshot initial values so we can report what changed on Done.
        self._initial_specs = [s for s in Options.spec() if s.scope >= options._scope]
        self._initial_values: dict[str, Any] = {
            spec.resolved_name: options.get(spec)
            for spec in self._initial_specs
        }

    def compose(self) -> ComposeResult:
        scope_label = "root" if self._options._scope == OptionScope.Root else "session"

        yield Button("x", id="options-dismiss")
        with Vertical():
            yield Static(f"Options ({scope_label})", id="options-title")

            top_level, nodes = Options.spec_tree()
            first = True

            # Top-level options (no namespace)
            for spec in top_level:
                if spec.scope < self._options._scope:
                    continue
                first = False
                yield from self._yield_option_row(spec)

            # Namespace nodes
            for node in nodes:
                yield from self._yield_node(node, first)
                first = False

            yield Button("done", id="options-done")

    def _yield_option_row(self, spec: OptionSpec):
        """Yield widgets for a single option row."""
        wid = _sanitize_id(spec.resolved_name)
        self._widget_specs[wid] = spec
        current = self._options.get(spec)

        with Horizontal(classes="option-row"):
            with Vertical(classes="option-info"):
                yield Label(spec.resolved_name, classes="option-name")
                yield Label(spec.help, classes="option-desc")
            yield _build_widget(spec, current, wid, self._options)

    def _yield_node(self, node: OptionNamespaceNode, first: bool):
        """Recursively yield widgets for a namespace node."""
        # Filter to specs visible at current scope
        visible = [s for s in node.options if s.scope >= self._options._scope]
        visible_children = any(
            any(s.scope >= self._options._scope for s in c.options) or c.children
            for c in node.children
        )
        if not visible and not visible_children:
            return

        if not first:
            yield Rule()

        ns = node.namespace
        yield Static(ns.resolved_name, classes="option-group-title")
        if ns.description:
            yield Label(ns.description, classes="option-desc")

        for spec in visible:
            yield from self._yield_option_row(spec)

        for child in node.children:
            yield from self._yield_node(child, False)

    def on_mount(self) -> None:
        """Subscribe to condition specs so dependent widgets update in-place."""
        for spec in Options.spec():
            if isinstance(spec, ConditionalChoicesOptionSpec):

                async def _update_dependent(
                    old: Any, new: Any, dep: ConditionalChoicesOptionSpec = spec
                ) -> None:
                    wid = _sanitize_id(dep.resolved_name)
                    try:
                        select = self.query_one(f"#{wid}", Select)
                    except Exception:
                        return
                    choices = dep.choices_for(new)
                    select.set_options([(str(c), c) for c in choices])
                    new_val = self._options.get(dep)
                    if new_val in choices:
                        select.value = new_val
                    else:
                        select.value = choices[0] if choices else Select.NULL

                self._options.subscribe(spec.condition, _update_dependent)

            elif isinstance(spec, ConditionalIntRangeOptionSpec):

                async def _update_range_dependent(
                    old: Any, new: Any, dep: ConditionalIntRangeOptionSpec = spec
                ) -> None:
                    wid = _sanitize_id(dep.resolved_name)
                    try:
                        inp = self.query_one(f"#{wid}", Input)
                    except Exception:
                        return
                    lo, hi = dep.range_for(new)
                    inp.placeholder = f"{lo}-{hi}"
                    inp.value = str(self._options.get(dep))

                self._options.subscribe(spec.condition, _update_range_dependent)

    def _spec_for_widget(self, widget_id: str | None) -> OptionSpec | None:
        """Look up the OptionSpec associated with a widget, if any."""
        if widget_id is None or not widget_id.startswith("opt-"):
            return None
        return self._widget_specs.get(widget_id)

    def on_select_changed(self, event: Select.Changed) -> None:
        spec = self._spec_for_widget(event.select.id)

        if spec is None or event.value == Select.NULL:
            return

        self._set_option(spec, event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        spec = self._spec_for_widget(event.input.id)

        if spec is None:
            return

        # Validate eagerly so we can revert the input widget on failure
        # (the worker would swallow the exception otherwise).
        try:
            val = spec.validate(event.value)
        except ValueError:
            event.input.value = str(self._options.get(spec))
            return

        event.input.value = str(val)
        self._set_option(spec, val)

    def _set_option(self, spec: OptionSpec, value: Any) -> None:
        """Set an option value via the Options pub/sub system."""

        async def _do_set() -> None:
            await self._options.set(spec, value)

        self.run_worker(_do_set())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "options-dismiss":
            self.post_message(WidgetDeactivated(self))
            self.post_message(self.Dismissed())
        elif event.button.id == "options-done":
            self._flush_inputs()
            changes: dict[str, tuple[Any, Any]] = {}
            for spec in self._initial_specs:
                old = self._initial_values[spec.resolved_name]
                new = self._options.get(spec)
                if new != old:
                    changes[spec.resolved_name] = (old, new)
            self.post_message(WidgetDeactivated(self))
            self.post_message(self.Done(changes))

    def _flush_inputs(self) -> None:
        """Apply any pending Input values that haven't been submitted via Enter."""
        for widget_id, spec in self._widget_specs.items():
            if not isinstance(spec, (IntRangeOptionSpec, ConditionalIntRangeOptionSpec)):
                continue
            try:
                inp = self.query_one(f"#{widget_id}", Input)
            except Exception:
                continue
            try:
                val = spec.validate(inp.value)
            except ValueError:
                continue
            current = self._options.get(spec)
            if val != current:
                self._set_option(spec, val)
