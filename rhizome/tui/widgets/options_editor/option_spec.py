from typing import Any

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Select, Static
from textual.widgets._select import SelectOverlay

from rhizome.app.options import (
    ChoicesOptionSpec,
    ConditionalChoicesOptionSpec,
    ConditionalIntRangeOptionSpec,
    IntRangeOptionSpec,
    FloatRangeOptionSpec,
    OptionSpec,
    ToggleOptionSpec,
)
from rhizome.app.options_editor import OptionsEditorModel
from rhizome.tui.keybindings import Keybind
from rhizome.tui.widgets.shared.text_area import ConfirmableTextArea


class OptionSpecView(Widget, can_focus=True):
    # Focus-state is tracked manually via the ``osv-focused`` class rather than the
    # ``:focus-within`` pseudo-selector. Pseudo-classes force Textual to invalidate
    # every ancestor on focus changes anywhere in the tree; a class selector is local
    # to the node that flipped.

    DEFAULT_CSS = """
    OptionSpecView {
        layout: horizontal;
        height: auto;
        padding: 0 2 1 2;
        background: rgb(20, 20, 20);
    }
    OptionSpecView.osv-focused {
        background: rgb(32, 32, 32);
    }
    OptionSpecView.osv-invalid {
        background: rgb(80, 30, 30);
    }
    OptionSpecView #osv-summary {
        width: 1fr;
        background: transparent;
    }
    """

    def __init__(self, vm: OptionsEditorModel, spec: OptionSpec, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._vm = vm
        self._spec = spec

    @property
    def vm(self) -> OptionsEditorModel:
        return self._vm

    @property
    def spec(self) -> OptionSpec:
        return self._spec

    def compose(self) -> ComposeResult:
        yield Static(self._summary_text(), id="osv-summary")

    @property
    def summary(self):
        try:
            return self.query_one("#osv-summary", Static)
        except Exception:
            return None

    def _summary_text(self) -> Text:
        text = Text()

        if self.has_class("osv-focused"):
            text.append("► ", style="bold #ffd700")
            text.append(self.spec.resolved_name + "\n", style="#d0d0d0")
            text.append(self.spec.help, style="#909090")
        else:
            text.append(self.spec.resolved_name + "\n", style="#a0a0a0")
            text.append(self.spec.help, style="#707070")

        return text

    def on_focus(self) -> None:
        self.add_class("osv-focused")
        if self.summary:
            self.summary.update(self._summary_text())

    def on_blur(self) -> None:
        self.remove_class("osv-focused")
        if self.summary:
            self.summary.update(self._summary_text())

    # ------------------------------------------------------------------
    # VM dirty subscription — repaints the value display on external mutations
    # (reset, agent-side flips, conditional cascades). Subscribed during the
    # widget's mount lifetime so the cleanup matches the OptionsEditorModel's own
    # detach() that runs on editor unmount.
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        self.vm.subscribe(self.vm.Callbacks.OnDirty, self._on_vm_dirty)

    def on_unmount(self) -> None:
        self.vm.unsubscribe(self.vm.Callbacks.OnDirty, self._on_vm_dirty)

    def _on_vm_dirty(self) -> None:
        self._refresh_value()

    def _refresh_value(self) -> None:
        """Subclass hook: repaint per-row value display when VM state changes."""

    async def commit(self, value: Any) -> bool:
        try:
            await self.vm.set_value(self.spec, value)
            return True
        except ValueError:
            self._flash_invalid()
            return False

    def _flash_invalid(self) -> None:
        self.add_class("osv-invalid")
        self.set_timer(0.35, lambda: self.remove_class("osv-invalid"))


# ======================================================================================================
# Choices — Select-based editor (lazy mount)
# ======================================================================================================


class _OptionSelect(Select):
    # Select's @on handler for SelectOverlay.Dismiss always calls event.stop(), and MRO
    # dispatch runs it regardless of subclass overrides, so we re-emit a fresh Dismissed
    # message that bubbles cleanly to the parent SelectOptionSpecView. Both user flows
    # (esc and option-pick) end here: option-pick produces Dismiss(lost_focus=True) via
    # the overlay losing focus when Select grabs it back inside _update_selection.

    class Dismissed(Message):
        pass

    @on(SelectOverlay.Dismiss)
    def _emit_dismissed(self, event: SelectOverlay.Dismiss) -> None:
        self.post_message(self.Dismissed())


class SelectOptionSpecView(OptionSpecView):
    # Renders a cheap Static label by default and only mounts an _OptionSelect when the
    # user activates ``enter`` to edit. Keeping the Select unmounted while idle avoids
    # per-row stylesheet cost from Select's substantial DEFAULT_CSS and its child widgets
    # (SelectOverlay, SelectCurrent), which dominates focus/cursor latency at list scale.

    DEFAULT_CSS = """
    SelectOptionSpecView #osv-value {
        max-width: 30;
        dock: right;
        background: transparent;
        color: #d0d0d0;
    }
    SelectOptionSpecView _OptionSelect {
        max-width: 30;
        dock: right;
    }
    """

    BINDINGS = [Keybind.MenuConfirm.as_binding("edit", show=False)]

    def compose(self) -> ComposeResult:
        yield from super().compose()
        yield Static(self._value_text(), id="osv-value")

    @staticmethod
    def _choices_for(
        vm: OptionsEditorModel,
        spec: OptionSpec
    ) -> list[tuple[str, Any]]:
        if isinstance(spec, ConditionalChoicesOptionSpec):
            condition_value = vm.get(spec.condition)
            return [(str(c), c) for c in spec.choices_for(condition_value)]
        assert isinstance(spec, ChoicesOptionSpec)
        return [(str(c), c) for c in spec.choices]

    def _value_text(self) -> str:
        return str(self.vm.get(self.spec))

    def _refresh_value(self) -> None:
        if self.value_label:
            self.value_label.update(self._value_text())

    @property
    def value_label(self):
        try:
            return self.query_one("#osv-value", Static)
        except Exception:
            return None

    @property
    def select(self):
        try:
            return self.query_one("#osv-select", _OptionSelect)
        except Exception:
            return None

    async def action_edit(self) -> None:
        if self.select is not None:
            return

        label = self.value_label
        if label is not None:
            label.display = False

        select = _OptionSelect(
            self._choices_for(self.vm, self.spec),
            value=self.vm.get(self.spec),
            compact=True,
            allow_blank=False,
            id="osv-select",
        )
        await self.mount(select)
        select.focus()
        select.expanded = True

    @on(_OptionSelect.Dismissed)
    async def _on_select_dismissed(self, event: _OptionSelect.Dismissed) -> None:
        event.stop()

        select = self.select
        if select is not None:
            new_value = select.value
            await select.remove()
            if new_value != self.vm.get(self.spec):
                await self.commit(new_value)

        label = self.value_label
        if label is not None:
            label.display = True

        self.focus()



class _OptionTextArea(ConfirmableTextArea):
    # Compact one-line text input for range edits. Construct with ``compact=True`` so
    # TextArea's default ``tall`` border (and the focused ``:focus`` border that would
    # otherwise re-inflate height to 3 cells) is suppressed; ``height: 1`` then keeps it
    # on a single line. Inherits ctrl+j → AcceptEditsRequested from ConfirmableTextArea;
    # additionally accepts plain ``enter`` and emits CancelEditsRequested on escape so
    # the parent view can tear down without committing. Mirrors RenameTextArea. Width /
    # docking are deliberately left to the parent view since this widget is mounted into
    # a Horizontal RHS container alongside a persistent hint Static.

    DEFAULT_CSS = """
    _OptionTextArea {
        width: 1fr;
        height: 1;
        background: rgb(40, 40, 40);
        scrollbar-size-horizontal: 0;
        scrollbar-size-vertical: 0;
    }
    """

    BINDINGS = [
        Keybind.MenuConfirm.as_binding("accept_edits", show=False),
        Keybind.CloseMenu.  as_binding("cancel_edits", show=False),
    ]

    class CancelEditsRequested(Message):
        pass

    def action_cancel_edits(self) -> None:
        self.post_message(self.CancelEditsRequested())

    async def _on_key(self, event: events.Key) -> None:
        # TextArea intercepts ``enter`` (newline insert) and ``escape`` (focus next) before
        # the binding system runs; short-circuit both to route them to our actions.
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.action_accept_edits()
            return
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.action_cancel_edits()
            return
        await super()._on_key(event)


class TextEditOptionSpecView(OptionSpecView):
    # Shared lazy-mount editor backed by ``_OptionTextArea`` — covers any spec whose value
    # can be edited as freeform text and validated via ``spec.from_string``. The RHS is a
    # Horizontal container holding a value Static (or the editor that replaces it) plus a
    # persistent hint Static. Subclasses decide how to render the value and optionally
    # supply a hint like "min-max" by overriding ``_format_value`` / ``_hint``.
    # Two dock:right siblings would overlap (Textual places each at the same right edge),
    # so the Horizontal container is what gives us value-left-of-hint side-by-side.

    DEFAULT_CSS = """
    TextEditOptionSpecView #osv-rhs {
        dock: right;
        width: 30;
        height: auto;
        layout: horizontal;
        background: transparent;
    }
    TextEditOptionSpecView #osv-value {
        width: 1fr;
        background: transparent;
        color: #d0d0d0;
    }
    TextEditOptionSpecView #osv-hint {
        width: auto;
        background: transparent;
        color: #707070;
    }
    """

    BINDINGS = [Keybind.MenuConfirm.as_binding("edit", show=False)]

    def compose(self) -> ComposeResult:
        yield from super().compose()
        with Horizontal(id="osv-rhs"):
            yield Static(self._value_text(), id="osv-value")
            yield Static(self._hint_display(), id="osv-hint")

    def _value_text(self) -> str:
        return self._format_value(self.vm.get(self.spec))

    def _hint_display(self) -> str:
        hint = self._hint()
        return f"({hint})" if hint else ""

    def _format_value(self, value: Any) -> str:
        return str(value)

    def _hint(self) -> str:
        """Optional "min-max"-style hint string rendered next to the value. Empty = none."""
        return ""

    def _refresh_value(self) -> None:
        if self.value_label:
            self.value_label.update(self._value_text())
        if self.hint_label:
            self.hint_label.update(self._hint_display())

    @property
    def value_label(self):
        try:
            return self.query_one("#osv-value", Static)
        except Exception:
            return None

    @property
    def hint_label(self):
        try:
            return self.query_one("#osv-hint", Static)
        except Exception:
            return None

    @property
    def editor(self):
        try:
            return self.query_one("#osv-editor", _OptionTextArea)
        except Exception:
            return None

    async def action_edit(self) -> None:
        if self.editor is not None:
            return

        label = self.value_label
        if label is None:
            return
        rhs = self.query_one("#osv-rhs", Horizontal)
        label.display = False

        editor = _OptionTextArea(text=str(self.vm.get(self.spec)), id="osv-editor", compact=True)
        # Mount before the (hidden) value Static so the editor sits in the value's slot,
        # with the hint Static still rendered to its right.
        await rhs.mount(editor, before=label)
        editor.focus()
        editor.action_select_all()

    @on(ConfirmableTextArea.AcceptEditsRequested)
    async def _on_accept(self, event: ConfirmableTextArea.AcceptEditsRequested) -> None:
        event.stop()
        editor = self.editor
        if editor is None:
            return

        raw = editor.text
        try:
            value = self.spec.from_string(raw)
        except ValueError:
            self._flash_invalid()
            await self._end_edit()
            return

        if value != self.vm.get(self.spec):
            await self.commit(value)
        await self._end_edit()

    @on(_OptionTextArea.CancelEditsRequested)
    async def _on_cancel(self, event: _OptionTextArea.CancelEditsRequested) -> None:
        event.stop()
        await self._end_edit()

    async def _end_edit(self) -> None:
        editor = self.editor
        if editor is not None:
            await editor.remove()
        label = self.value_label
        if label is not None:
            label.display = True
        self.focus()


class StringOptionSpecView(TextEditOptionSpecView):
    # Plain string editor — no range hint, ``OptionSpec.from_string`` just strips whitespace.
    pass


class IntRangeOptionSpecView(TextEditOptionSpecView):

    def _hint(self) -> str:
        assert isinstance(self.spec, IntRangeOptionSpec)
        return f"{self.spec.min}-{self.spec.max}"


class FloatRangeOptionSpecView(TextEditOptionSpecView):

    def _format_value(self, value: Any) -> str:
        return f"{float(value):g}"

    def _hint(self) -> str:
        assert isinstance(self.spec, FloatRangeOptionSpec)
        return f"{self.spec.min:g}-{self.spec.max:g}"


class ConditionalIntRangeOptionSpecView(TextEditOptionSpecView):

    def _hint(self) -> str:
        assert isinstance(self.spec, ConditionalIntRangeOptionSpec)
        condition_value = self.vm.get(self.spec.condition)
        try:
            lo, hi = self.spec.range_for(condition_value)
        except KeyError:
            return "?"
        return f"{lo}-{hi}"


class ToggleOptionSpecView(OptionSpecView):
    # Toggle is rendered as "[x] Enabled" / "[ ] Disabled" docked right. Pressing enter
    # flips the value through ``commit`` directly — no overlay, no text-area, no lazy mount.

    DEFAULT_CSS = """
    ToggleOptionSpecView #osv-value {
        max-width: 30;
        dock: right;
        background: transparent;
        color: #d0d0d0;
    }
    """

    BINDINGS = [Keybind.MenuConfirm.as_binding("toggle", show=False)]

    def compose(self) -> ComposeResult:
        yield from super().compose()
        # Static accepts a Rich Text directly, which sidesteps Rich's markup parser — the
        # literal ``[x]`` would otherwise be eaten as a style block.
        yield Static(self._value_text(), id="osv-value")

    def _value_text(self) -> Text:
        enabled = self.vm.get(self.spec) == "enabled"
        text = Text()
        if enabled:
            text.append("[x]", style="bold #67c267")
            text.append(" Enabled", style="#d0d0d0")
        else:
            text.append("[ ]", style="#a0a0a0")
            text.append(" Disabled", style="#707070")
        return text

    def _refresh_value(self) -> None:
        if self.value_label:
            self.value_label.update(self._value_text())

    @property
    def value_label(self):
        try:
            return self.query_one("#osv-value", Static)
        except Exception:
            return None

    async def action_toggle(self) -> None:
        current = self.vm.get(self.spec)
        await self.commit("disabled" if current == "enabled" else "enabled")


# ======================================================================================================
# Dispatch
# ======================================================================================================


def make_option_spec_view(vm: OptionsEditorModel, spec: OptionSpec) -> OptionSpecView:
    # Order matters: ToggleOptionSpec is a ChoicesOptionSpec subclass and must be checked
    # first; bare ``OptionSpec`` is the freeform-string fallback and lands on
    # ``StringOptionSpecView`` rather than the read-only base view.
    if isinstance(spec, ToggleOptionSpec):
        return ToggleOptionSpecView(vm, spec)
    if isinstance(spec, (ChoicesOptionSpec, ConditionalChoicesOptionSpec)):
        return SelectOptionSpecView(vm, spec)
    if isinstance(spec, ConditionalIntRangeOptionSpec):
        return ConditionalIntRangeOptionSpecView(vm, spec)
    if isinstance(spec, IntRangeOptionSpec):
        return IntRangeOptionSpecView(vm, spec)
    if isinstance(spec, FloatRangeOptionSpec):
        return FloatRangeOptionSpecView(vm, spec)
    return StringOptionSpecView(vm, spec)
