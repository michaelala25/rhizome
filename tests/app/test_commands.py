"""Tests for the hand-rolled command registry (rhizome/app/commands.py)."""

import pytest

from rhizome.app.commands import (
    NULLARY,
    RAW,
    Command,
    CommandRegistry,
    CommandUsageError,
    DefaultParser,
    Flag,
    UnknownCommandError,
)


# ----- parsers ------------------------------------------------------------- #


def test_nullary_ignores_remainder():
    assert NULLARY.parse("anything at all").args == ()
    assert NULLARY.parse("anything at all").kwargs == {}


def test_raw_keeps_quotes_and_apostrophes():
    # The whole point: no shlex, so "Can't" never blows up and quotes survive.
    parsed = RAW.parse("  Can't Stop, Won't Stop  ")
    assert parsed.args == ("Can't Stop, Won't Stop",)


def test_default_parser_peels_flags_then_rejoins_rest():
    parser = DefaultParser(flags=[Flag("auto")], rest=True)
    parsed = parser.parse("--auto draft from the conversation")
    assert parsed.kwargs == {"auto": True}
    assert parsed.args == ("draft from the conversation",)


def test_default_parser_short_flag_and_keyword_dest():
    parser = DefaultParser(flags=[Flag("global", short="-g")])
    assert parser.parse("-g").kwargs == {"global_": True}      # 'global' is a keyword -> 'global_'
    assert parser.parse("").kwargs == {"global_": False}


def test_default_parser_rejects_unknown_flag_and_stray_args():
    parser = DefaultParser(flags=[Flag("auto")])
    with pytest.raises(CommandUsageError):
        parser.parse("--nope")
    with pytest.raises(CommandUsageError):
        parser.parse("unexpected positional")   # rest=False


# ----- registry dispatch --------------------------------------------------- #


async def test_execute_nullary_and_raw():
    reg = CommandRegistry()
    seen = {}

    def _clear():
        seen["clear"] = True

    def _rename(name):
        seen["rename"] = name

    reg.register("clear", _clear, help="Clear the feed.")
    reg.register("rename", _rename, help="Rename.", parser=RAW)

    assert await reg.execute("/clear") is None
    assert seen["clear"] is True

    await reg.execute("/rename My Tab's Name")
    assert seen["rename"] == "My Tab's Name"


async def test_execute_awaits_coroutine_handlers_and_returns_text():
    reg = CommandRegistry()

    async def _echo(text):
        return f"echoed: {text}"

    reg.register("echo", _echo, help="Echo.", parser=RAW)
    assert await reg.execute("/echo hello") == "echoed: hello"


async def test_unknown_command_raises_clean_message():
    reg = CommandRegistry()
    with pytest.raises(UnknownCommandError) as exc:
        await reg.execute("/nope")
    assert str(exc.value) == "Unknown command: /nope"


async def test_flag_command_dispatch():
    reg = CommandRegistry()
    captured = {}

    def _options(*, global_):
        captured["global"] = global_

    reg.register("options", _options, help="Options.", parser=DefaultParser(flags=[Flag("global", short="-g")]))
    await reg.execute("/options --global")
    assert captured["global"] is True
    await reg.execute("/options")
    assert captured["global"] is False


# ----- scope merging (global <- session) ----------------------------------- #


def test_child_scope_merges_and_shadows_parent():
    parent = CommandRegistry()
    parent.add(Command("quit", handler=lambda: None, help="Quit."))
    parent.add(Command("new", handler=lambda: None, help="New tab."))

    child = CommandRegistry(parent=parent)
    child.add(Command("clear", handler=lambda: None, help="Clear."))
    child.add(Command("quit", handler=lambda: None, help="Child quit override."))

    names = [name for name, _ in child.rows()]
    assert names == ["clear", "new", "quit"]                       # merged + sorted
    assert child.resolve("new") is parent.resolve("new")           # falls through to parent
    assert child.resolve("quit").help == "Child quit override."    # child shadows parent


async def test_child_dispatch_falls_through_to_parent():
    parent = CommandRegistry()
    hits = {}
    parent.add(Command("quit", handler=lambda: hits.setdefault("quit", True), help="Quit."))
    child = CommandRegistry(parent=parent)

    await child.execute("/quit")
    assert hits["quit"] is True


# ----- built-in help ------------------------------------------------------- #


async def test_help_overview_lists_merged_commands():
    parent = CommandRegistry()
    parent.add(Command("quit", handler=lambda: None, help="Quit the app."))
    child = CommandRegistry(parent=parent)
    child.add(Command("clear", handler=lambda: None, help="Clear the feed."))

    overview = await child.execute("/help")
    assert "/quit" in overview and "/clear" in overview


async def test_help_for_single_command_shows_usage_and_flags():
    reg = CommandRegistry()
    reg.add(Command(
        "options", handler=lambda *, global_=False: None, help="Edit options.",
        parser=DefaultParser(flags=[Flag("global", short="-g", help="Target global options.")]),
    ))

    detail = await reg.execute("/help options")
    assert "Usage: /options [--global]" in detail
    assert "Target global options." in detail

    # Trailing --help routes to the same per-command help.
    via_flag = await reg.execute("/options --help")
    assert "Usage: /options [--global]" in via_flag
