"""InterruptWidgetBase — abstract base for interrupt widgets."""

from __future__ import annotations

import asyncio
from typing import Any

from textual.binding import Binding
from textual.widget import Widget

from .navigable import NavigableWidgetMixin


class InterruptWidgetBase(NavigableWidgetMixin, Widget, can_focus=True):
    """Base class for future-based interrupt widgets with navigation support.

    Subclasses get ``_future``, ``resolve()``, ``wait_for_selection()``,
    ``cancel()``, and the full ``NavigableWidgetMixin`` lifecycle for free.
    Ctrl+C cancels the pending future when the widget has focus.
    """

    BINDINGS = [
        Binding("ctrl+c", "cancel_interrupt", "Cancel", show=False),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()

    def on_mount(self) -> None:
        self.setup_navigation()

    def is_navigable(self) -> bool:
        return not self._future.done()

    def resolve(self, result: Any, deactivate_navigation: bool = True) -> None:
        """Set the future result and (optionally) deactivate the widget."""
        if self._future.done():
            return
        self._future.set_result(result)
        if deactivate_navigation:
            self.deactivate_navigation()

    async def wait_for_selection(self) -> Any:
        """Block until the user resolves the interrupt. Returns the result value."""
        return await self._future

    def cancel(self) -> None:
        """Cancel the pending future if not yet resolved."""
        if not self._future.done():
            self._future.cancel()
            self.deactivate_navigation()

    def action_cancel_interrupt(self) -> None:
        """Handle ctrl+c — cancel the pending future."""
        self.cancel()
