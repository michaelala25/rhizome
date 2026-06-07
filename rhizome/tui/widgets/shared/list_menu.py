"""ListMenu — focusable list of MenuItems with arrow nav + enter to select."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal

from rich.text import Text
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Static

from rhizome.tui.keybindings import Keybind


Orientation = Literal["horizontal", "vertical"]


@dataclass
class MenuItem:
    """A single entry in a ``ListMenu``. ``key`` and ``desc`` are display metadata only — the
    parent view binds keys at its own level."""

    label: str
    key: Keybind | None = None
    desc: str | None = None

    def render(self, focused: bool, selected: bool) -> Text | None:
        """Per-item rendering hook. Returning ``None`` falls back to the host menu's
        ``_render_item``."""
        return None


class ListMenu(Static, can_focus=True):
    """Focusable list of items with arrow nav, enter to select, escape to dismiss.

    Posts ``Selected(item)`` on enter and ``Dismiss`` on escape. The parent view decides what
    each item means.
    """

    ITEMS: ClassVar[list[MenuItem]] = []
    """Items shown in the menu, in display order."""

    ORIENTATION: ClassVar[Orientation] = "horizontal"
    """Cursor axis. Horizontal menus use left/right; vertical menus use up/down."""

    WRAP: ClassVar[bool] = False
    """Wrap the cursor at boundaries instead of bubbling the arrow key to the parent."""

    HEADER: ClassVar[str | None] = None
    """Optional line above the items."""

    LEAD: ClassVar[str | None] = None
    """Optional dim inline prefix before the first item."""

    HINT: ClassVar[str | None] = None
    """Optional dim line below the items."""

    _label_pad_width: int = 0
    _key_pad_width:   int = 0
    """Column widths used to left-align keychords, labels, and descriptions across rows. Computed
    in ``_refresh`` when the menu is vertical and at least one item carries a key or description;
    ``0`` (no padding) otherwise."""

    DEFAULT_CSS = """
    ListMenu {
        height: auto;
        padding: 0 1;
        background: transparent;
    }
    """

    BINDINGS = [
        Keybind.CursorLeft. as_binding("cursor_left",  show=False),
        Keybind.CursorRight.as_binding("cursor_right", show=False),
        Keybind.CursorUp.   as_binding("cursor_up",    show=False),
        Keybind.CursorDown. as_binding("cursor_down",  show=False),
        Keybind.MenuConfirm.as_binding("select",       show=False),
        Keybind.CloseMenu.  as_binding("cancel",       show=False),
    ]

    cursor: reactive[int] = reactive(0)
    """Index of the currently-highlighted item. Writes are watched — parent widgets can assign
    ``menu.cursor = n`` (e.g. when handing focus into the menu) and the display updates."""

    class Dismiss(Message):
        """Posted when the user presses escape on the menu."""

    @dataclass
    class Selected(Message):
        """Posted when the user presses enter on the cursor item."""
        item: MenuItem

    def on_mount(self) -> None:
        self._refresh()

    def on_focus(self) -> None:
        self.call_after_refresh(self._refresh)

    def on_blur(self) -> None:
        self.call_after_refresh(self._refresh)

    @property
    def items(self) -> list[MenuItem]:
        return self.ITEMS

    @property
    def header(self) -> Text | None:
        return Text(self.HEADER) if self.HEADER is not None else None

    @property
    def lead(self) -> Text | None:
        return Text(self.LEAD, style="dim") if self.LEAD is not None else None

    @property
    def hint(self) -> Text | None:
        return Text(self.HINT, style="dim") if self.HINT is not None else None

    def prepare_for_show(self) -> None:
        """Reset the cursor to the first item. Call from the parent on each show so a fresh open
        lands on the most-likely-default action."""
        self.cursor = 0
        self._refresh()

    def watch_cursor(self, _value: int) -> None:
        self._refresh()

    def _refresh(self) -> None:
        items = self.items

        is_vertical = self.ORIENTATION == "vertical"
        any_key     = any(i.key is not None for i in items)
        any_desc    = any(i.desc           for i in items)

        if is_vertical and (any_key or any_desc):
            self._label_pad_width = max(len(i.label) for i in items)
            self._key_pad_width   = max(
                (len(i.key.default_key) for i in items if i.key is not None),
                default=0,
            )
        else:
            self._label_pad_width = 0
            self._key_pad_width   = 0

        text = Text()
        if (header := self.header):
            text.append(header + "\n")
        if (lead := self.lead):
            text.append(lead)

        sep = "\n" if self.ORIENTATION == "vertical" else "   "
        for i, item in enumerate(items):
            selected = (self.cursor == i)
            item_text = item.render(focused=self.has_focus, selected=selected)
            if item_text is None:
                item_text = self._render_item(item, selected)
            text.append(item_text)
            if i < len(items) - 1:
                text.append(sep)

        if (hint := self.hint):
            text.append("\n")
            text.append(hint)

        self.update(text)

    def _render_item(self, item: MenuItem, selected: bool) -> Text:
        """Default rendering: ``► key  label  - desc``. Each of ``key`` and the ``  - desc``
        suffix are present iff the corresponding ``MenuItem`` field is set. On vertical menus
        where at least one item carries a key or description, every row's chord and label
        columns are padded to ``_key_pad_width`` / ``_label_pad_width`` so the columns line up
        on the left. Override on a subclass for a fully-custom layout."""
        focused = self.has_focus
        text = Text()

        if selected:
            text.append("► ", style="bold #ffd700" if focused else "bold #707070")
        else:
            text.append("  ")

        if self._key_pad_width:
            keybind = item.key.default_key if item.key is not None else ""
            text.append(f"{keybind:<{self._key_pad_width}}", style="#a0a0a0")
            text.append("  ")
        elif item.key is not None:
            text.append(item.key.default_key, style="#a0a0a0")
            text.append("  ")

        label_style = "bold white" if (selected and focused) else "#a0a0a0"
        if self._label_pad_width:
            text.append(f"{item.label:<{self._label_pad_width}}", style=label_style)
        else:
            text.append(item.label, style=label_style)

        if item.desc:
            desc_style = "#909090" if (selected and focused) else "#707070"
            text.append(f"  - {item.desc}", style=desc_style)

        return text

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        # Orthogonal-axis cursor actions never bind locally.
        if (
            (self.ORIENTATION == "horizontal" and action in ("cursor_up", "cursor_down")) or
            (self.ORIENTATION == "vertical"   and action in ("cursor_left", "cursor_right"))
        ):
            return False

        if self.WRAP:
            return True

        n = len(self.items)
        if action in ("cursor_up", "cursor_left"):
            return self.cursor > 0
        if action in ("cursor_down", "cursor_right"):
            return n > 0 and self.cursor < n - 1
        return True

    def action_cursor_left(self) -> None:
        self._move(-1)

    def action_cursor_right(self) -> None:
        self._move(1)

    def action_cursor_up(self) -> None:
        self._move(-1)

    def action_cursor_down(self) -> None:
        self._move(1)

    def _move(self, delta: int) -> None:
        n = len(self.items)
        if n == 0:
            return
        if self.WRAP:
            self.cursor = (self.cursor + delta) % n
        else:
            self.cursor = max(0, min(self.cursor + delta, n - 1))

    def action_select(self) -> None:
        if not self.items:
            return
        self.post_message(ListMenu.Selected(self.items[self.cursor]))

    def action_cancel(self) -> None:
        self.post_message(ListMenu.Dismiss())
