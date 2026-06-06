
from typing import Any

from rich.text import Text
from textual.actions import SkipAction
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.events import DescendantFocus, Focus
from textual.widgets import Rule, Static

from rhizome.app.options_editor import OptionsEditorModel
from rhizome.tui.keybindings import Keybind
from rhizome.app.options import (
    OptionNamespace,
    OptionNamespaceNode,
    OptionSpec
)

from rhizome.tui.widgets.options_editor.option_spec import (
    make_option_spec_view,
    OptionSpecView
)

class OptionsListContainer(VerticalScroll):

    DEFAULT_CSS = """
    OptionsListContainer {
        height: auto;
        max-height: 40;
        border: solid #3a3a3a;
        background: transparent;
    }
    OptionsListContainer Rule.oe-ns-rule {
        height: 1;
        margin: 0;
        color: #3a3a3a;
        background: transparent;
    }
    OptionsListContainer .oe-ns-header {
        padding: 0 1 1 1;
        background: transparent;
    }
    """

    BINDINGS = [
        # Cursor navigation (rows arranged top-to-bottom with 0 at the top, hence up == -1)
        Keybind.CursorUp.  as_binding("navigate_cursor(-1)", show=False),
        Keybind.CursorDown.as_binding("navigate_cursor(1)",  show=False),
        Keybind.PageUp.    as_binding("navigate_cursor(-5)", show=False),
        Keybind.PageDown.  as_binding("navigate_cursor(5)",  show=False),
    ]

    def __init__(self, vm: OptionsEditorModel, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._vm = vm
        self._cursor: int | None = None
        self._option_rows: list[OptionSpecView] = []

    @property
    def vm(self):
        return self._vm

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        top, nodes = self.vm.visible_spec_tree()

        for spec in top:
            yield self._make_option_spec_view(spec)
        for node in nodes:
            yield from self._compose_namespace(node)

    def _compose_namespace(self, node: OptionNamespaceNode) -> ComposeResult:
        yield Rule(classes="oe-ns-rule")
        yield Static(self._namespace_header_text(node.namespace), classes="oe-ns-header")
        
        for spec in node.options:
            yield self._make_option_spec_view(spec)
        for child in node.children:
            yield from self._compose_namespace(child)

    def _make_option_spec_view(self, spec: OptionSpec) -> OptionSpecView:
        view = make_option_spec_view(self.vm, spec)
        self._option_rows.append(view)
        return view

    def _namespace_header_text(self, namespace: type[OptionNamespace]) -> Text:
        text = Text()
        # Title
        text.append(namespace.resolved_name, style="bold rgb(160,160,160)")
        # Description
        if namespace.description:
            text.append("\n" + namespace.description, style="rgb(112,112,112)")
        return text
    
    def on_mount(self):
        if self._option_rows:
            self._cursor = 0

    @property
    def current_option_row(self) -> OptionSpecView | None:
        if (
            self._option_rows and 
            self._cursor is not None and
            0 <= self._cursor < len(self._option_rows)
        ):
            return self._option_rows[self._cursor]
        return None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_focus(self, event: Focus) -> None:
        if self._option_rows and self._cursor is None:
            self._cursor = 0
        if self.current_option_row:
            self.current_option_row.focus()

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        # Update cursor if a row receives focus externally (e.g. mouse click)
        if isinstance(event.widget, OptionSpecView):
            if self.current_option_row is not event.widget and event.widget in self._option_rows:
                self._cursor = self._option_rows.index(event.widget)

    async def action_navigate_cursor(self, delta: int) -> None:
        if not self.current_option_row:
            # Either no options present or cursor not placed yet, nothing to do.
            return
        
        assert self._cursor is not None
        assert self._option_rows
        
        # At boundaries, with a delta of exactly +/-1, propagate to parent.
        if (
            (delta == -1 and self._cursor == 0) or
            (delta == 1 and self._cursor == len(self._option_rows) - 1)
        ):
            raise SkipAction()
        
        self._cursor = min(
            len(self._option_rows) - 1,
            max(
                0,
                self._cursor + delta
            )
        )
        self.current_option_row.focus()