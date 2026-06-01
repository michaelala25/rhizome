"""OptionsEditor VM — feed-mounted, present-or-absent editor for an ``Options`` instance.

Bound at the view layer in ``rhizome.tui.widgets.options_editor``. Holds a target ``Options``
plus a detached ``clone()`` of it as a staging buffer; ``apply()`` commits the diff back via
``target.merge_from(clone)``, ``discard()`` rebuilds the clone from current target state.

Immediate-flagged specs (``OptionSpec.immediate=True``) bypass staging and write straight to
the target — used for changes the user must see take effect live, e.g. theme.
"""

from rhizome.app.options_editor.options_editor import OptionsEditorVM

__all__ = ["OptionsEditorVM"]
