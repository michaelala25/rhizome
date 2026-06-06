"""``ResourceLinkerAccept`` — Accept / Cancel menu for pending staged link changes.

Bound to ``ResourceLinkerModel``. Reveals (``.-visible``) whenever the staging buffer diverges from the
linked baseline (``vm.is_dirty_staging``) and hides otherwise, so it only takes space while there's
something to commit. Accept commits the staged diff against the topic; Cancel reverts to the baseline.

Visibility + focus-orphan rescue live here, not on the parent: the widget is the linker VM's natural
listener, so it toggles its own class off the VM's ``dirty`` (``ChoiceList`` already subscribes), and
a dirty→clean transition that hides the menu out from under focus re-routes focus to the linker table.
"""

from __future__ import annotations

from rhizome.app.resource_viewer.linker import ResourceLinkerModel
from rhizome.tui.widgets.shared.choices_list import ChoiceList


class ResourceLinkerAccept(ChoiceList[ResourceLinkerModel]):
    CHOICES = {"Accept": "_accept", "Cancel": "_cancel"}
    LEAD = "Staged links: "

    async def _accept(self) -> None:
        await self._vm.accept()

    def _cancel(self) -> None:
        self._vm.cancel()

    def action_cancel(self) -> None:
        self._vm.cancel()

    def _refresh(self) -> None:
        super()._refresh()  # re-render the choice text (cursor brightness, etc.)

        # Visibility tracks the staging diff. ``on_mount`` calls this before focus exists, so the
        # rescue is naturally inert there (nothing's focused on us yet).
        dirty = self._vm is not None and self._vm.is_dirty_staging
        was_visible = self.has_class("-visible")
        self.set_class(dirty, "-visible")

        if was_visible and not dirty and self.screen is not None and self.screen.focused is self:
            try:
                self.screen.query_one("#rv-linker-table").focus()
            except Exception:
                pass
