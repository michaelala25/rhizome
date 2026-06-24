"""The command palette pulls from its CommandRegistryService lazily and across merged scopes."""

from rhizome.app.commands import Command, CommandRegistry
from rhizome.app.chat_area.command_palette import CommandPaletteModel


def _registry(*names: str) -> CommandRegistry:
    reg = CommandRegistry()
    for name in names:
        reg.add(Command(name, handler=lambda: None, help=f"{name} help"))
    return reg


def test_filtered_reads_live_from_the_service():
    reg = _registry("clear", "commit")
    palette = CommandPaletteModel(reg)
    palette.update_for_input("/c")

    assert [name for name, _ in palette.filtered] == ["clear", "commit"]

    # A command registered AFTER construction shows up without re-pushing anything — the palette pulls
    # rows() on demand rather than caching a snapshot.
    reg.add(Command("close", handler=lambda: None, help="close help"))
    assert [name for name, _ in palette.filtered] == ["clear", "close", "commit"]


def test_filtered_merges_inherited_global_scope():
    global_reg = _registry("quit", "new")
    session = CommandRegistry(parent=global_reg)
    session.add(Command("clear", handler=lambda: None, help="clear help"))

    palette = CommandPaletteModel(session)
    palette.update_for_input("/")
    assert [name for name, _ in palette.filtered] == ["clear", "new", "quit"]   # merged, sorted


def test_has_exact_match_uses_resolve_across_scopes():
    session = CommandRegistry(parent=_registry("quit"))
    session.add(Command("clear", handler=lambda: None))
    palette = CommandPaletteModel(session)

    assert palette.has_exact_match("/clear") is True
    assert palette.has_exact_match("/quit") is True       # inherited from the global parent
    assert palette.has_exact_match("/nope") is False


def test_visibility_and_cursor_track_input():
    palette = CommandPaletteModel(_registry("clear", "commit", "close"))

    palette.update_for_input("/c")
    assert palette.visible is True
    assert palette.selected_command == "clear"           # filtered rows are alphabetical

    palette.move_cursor(1)
    assert palette.selected_command == "close"

    palette.update_for_input("")          # left command mode
    assert palette.visible is False
