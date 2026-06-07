"""ContentEditor — stack of TextAreas with a shared Accept/Discard menu.

Subclasses declare ``AREAS`` as a ``{name: TextAreaParams}`` dict. Each key is the
unambiguous handle used everywhere (``set_text(name, …)``, ``event.text_areas[name]``); each
value carries per-area title / TextArea kwarg overrides.

The Accept/Discard menu is hidden when no area diverges from its seeded buffer and only takes
focus while ``self.dirty``. View-side state only — the parent seeds text via ``set_text(area,
text)`` and listens for ``ChangesAccepted`` / ``ChangesDiscarded``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, ClassVar

from textual import on
from textual.actions import SkipAction
from textual.containers import Vertical
from textual.events import Key
from textual.message import Message
from textual.widgets import TextArea

from rhizome.tui.keybindings import Keybind
from rhizome.tui.widgets.shared.focus_orchestration import FocusGraph, FocusOrchestrationMixin

from .list_menu import ListMenu, MenuItem


class ContentEditorMenu(ListMenu):
    """Accept/Discard menu shown by ``ContentEditor`` when dirty."""

    Accept  = MenuItem(label="Accept",  key=Keybind.EditAccept)
    Discard = MenuItem(label="Discard", key=Keybind.CloseMenu)

    ITEMS = [Accept, Discard]
    LEAD = "Edit: "


@dataclass
class TextAreaParams:
    """Per-area TextArea overrides.

    ``title`` overrides the displayed ``border_title`` (defaults to the area name itself).
    ``kwargs`` is merged on top of ``ContentEditor.DEFAULT_AREA_KWARGS`` and passed through to
    the ``TextArea`` constructor.
    """

    title: str | None = None
    kwargs: dict[str, Any] = field(default_factory=dict)


def _sanitize(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "x"


class ContentEditor(Vertical, FocusOrchestrationMixin):
    """Stack of TextAreas with a shared Accept/Discard menu, focus-orchestrated by alt+arrow."""

    AREAS: ClassVar[dict[str, "TextAreaParams"]] = {}
    """Editable areas, keyed by name in display order. The name doubles as the TextArea's
    ``border_title`` unless ``TextAreaParams.title`` overrides it."""

    DEFAULT_AREA_KWARGS: ClassVar[dict[str, Any]] = {
        "show_line_numbers": False,
        "soft_wrap": True,
    }
    """Default kwargs applied to every ``TextArea`` constructor. Per-area overrides live in
    ``AREAS[name].kwargs``."""

    can_focus = True

    DEFAULT_CSS = """
    ContentEditor {
        layout: vertical;
        height: auto;
        width: 1fr;
    }
    ContentEditor TextArea {
        background: transparent;
        border: solid #3a3a3a;
        border-title-align: right;
        border-title-color: rgb(120,120,120);
        height: auto;
        min-height: 3;
        max-height: 12;
        padding: 0 1;
    }
    ContentEditor TextArea:focus {
        border: solid $accent;
    }
    ContentEditor ContentEditorMenu {
        height: 3;
        margin: 1 0 0 0;
        padding: 0 1;
        border-top: solid #3a3a3a;
        color: rgb(200,200,200);
        display: none;
    }
    ContentEditor.-dirty ContentEditorMenu {
        display: block;
    }
    ContentEditor ContentEditorMenu:focus {
        border-top: solid $accent;
    }
    """

    BINDINGS = [
        Keybind.FocusUp.   as_binding("focus_up",     show=False),
        Keybind.FocusDown. as_binding("focus_down",   show=False),
        Keybind.EditAccept.as_binding("accept_edits", show=False, priority=True),
    ]

    @dataclass
    class ChangesAccepted(Message):
        """Posted when the user accepts the edits. ``text_areas`` is the dirty subset."""
        text_areas: dict[str, TextArea]

    @dataclass
    class ChangesDiscarded(Message):
        """Posted when the user discards the edits. ``text_areas`` is the pre-discard dirty subset."""
        text_areas: dict[str, TextArea]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._text_areas: dict[str, TextArea] = {}
        self._initial_buffers: dict[str, str] = {area: "" for area in self.AREAS}

    def compose(self):
        for area, params in self.AREAS.items():
            kwargs: dict[str, Any] = {**self.DEFAULT_AREA_KWARGS, **params.kwargs}
            kwargs.setdefault("id", self._area_id(area))

            text_area = TextArea(**kwargs)
            text_area.border_title = params.title if params.title is not None else area

            self._text_areas[area] = text_area
            yield text_area

        yield ContentEditorMenu(id=self._menu_id())

    @property
    def text_areas(self) -> dict[str, TextArea]:
        return self._text_areas

    @property
    def dirty(self) -> bool:
        return bool(self.dirty_areas)

    @property
    def dirty_areas(self) -> dict[str, TextArea]:
        return {
            area: ta
            for area, ta in self._text_areas.items()
            if ta.text != self._initial_buffers[area]
        }

    def set_text(self, area: str, text: str) -> None:
        """Seed ``area``'s buffer. Updates both the displayed text and the dirty-check baseline so
        a re-seed does not flag the area as dirty."""
        ta = self._text_areas.get(area)
        if ta is None:
            return
        if ta.text != text:
            ta.text = text
        self._initial_buffers[area] = text
        self._sync_dirty_class()

    def accept(self) -> None:
        dirty = self.dirty_areas
        if not dirty:
            return
        self.post_message(self.ChangesAccepted(text_areas=dirty))

    def discard(self) -> None:
        dirty = self.dirty_areas
        if not dirty:
            return
        for area, ta in dirty.items():
            ta.text = self._initial_buffers[area]
        self.post_message(self.ChangesDiscarded(text_areas=dirty))
        self._sync_dirty_class()

    @on(TextArea.Changed)
    def _on_text_area_changed(self, event: TextArea.Changed) -> None:
        self._sync_dirty_class()

    @on(ContentEditorMenu.Selected)
    def _on_menu_selected(self, event: ContentEditorMenu.Selected) -> None:
        match event.item:
            case ContentEditorMenu.Accept:  self.accept()
            case ContentEditorMenu.Discard: self.discard()

    @on(ContentEditorMenu.Dismiss)
    def _on_menu_dismissed(self, event: ContentEditorMenu.Dismiss) -> None:
        self.discard()

    def on_key(self, event: Key) -> None:
        # Escape from a TextArea (which doesn't bind it) bubbles here; the menu handles its own
        # escape via the Dismiss → _on_menu_dismissed path. ``discard`` is idempotent so a double
        # path is harmless.
        if event.key == "escape" and self.dirty:
            self.discard()
            event.stop()

    def action_focus_up(self) -> None:
        if self.focus_neighbour("up") is None:
            raise SkipAction()

    def action_focus_down(self) -> None:
        if self.focus_neighbour("down") is None:
            raise SkipAction()

    def action_accept_edits(self) -> None:
        if not self.dirty:
            raise SkipAction()
        self.accept()

    def _get_focus_graph(self) -> FocusGraph:
        # Use the *actual* widget ids — when ``AREAS[name].kwargs`` overrides ``id``, the mounted
        # TextArea's id differs from ``_area_id(name)`` and the focus graph must match what's in
        # the DOM, not the auto-generated default.
        area_ids = [self._text_areas[a].id for a in self.AREAS]
        menu_id = self._menu_id()
        edges: dict[str, dict[str, Any]] = {}
        for i, node in enumerate(area_ids):
            edges[node] = {}
            if i > 0:
                edges[node]["up"] = area_ids[i - 1]
            if i < len(area_ids) - 1:
                edges[node]["down"] = area_ids[i + 1]
            else:
                edges[node]["down"] = menu_id
        edges[menu_id] = {"up": area_ids[-1]} if area_ids else {}
        return FocusGraph(
            source=area_ids[0] if area_ids else menu_id,
            edges=edges,
        )

    def _is_node_available(self, node_id: str) -> bool:
        if node_id == self._menu_id():
            return self.dirty
        return True

    def _sync_dirty_class(self) -> None:
        self.set_class(self.dirty, "-dirty")

    def _area_id(self, area: str) -> str:
        return f"{type(self).__name__.lower()}-area-{_sanitize(area)}"

    def _menu_id(self) -> str:
        return f"{type(self).__name__.lower()}-menu"
