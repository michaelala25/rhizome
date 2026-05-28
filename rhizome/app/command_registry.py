"""Slash command parser and registry."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from shlex import split as shlex_split

import rich_click as click


@dataclass
class ParsedCommand:
    """Result of parsing a slash command from user input."""

    name: str
    args: str


def parse_input(text: str) -> ParsedCommand | None:
    """Parse user input into a command if it starts with ``/``.

    Returns ``None`` if the input is not a slash command (i.e. regular
    chat text).
    """
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None

    parts = stripped.split(maxsplit=1)
    name = parts[0][1:]  # drop the leading '/'
    args = parts[1] if len(parts) > 1 else ""
    return ParsedCommand(name=name, args=args)


class CommandRegistry:
    """Click-based command registry with async execution support.

    Commands are registered via ``@registry.command()`` and
    ``@registry.group()`` decorators.  ``execute()`` parses a raw
    command line, invokes the click command with
    ``standalone_mode=False``, and ``await``s async callbacks.
    """

    def __init__(self, max_content_width: int | None = None) -> None:
        self.commands: dict[str, click.BaseCommand] = {}
        self.max_content_width = max_content_width

    def command(self, *args, **kwargs):
        """Decorator: register a click command."""
        def decorator(func):
            cmd = click.command(*args, **kwargs)(func)
            self.commands[cmd.name] = cmd
            return cmd
        return decorator

    def group(self, *args, **kwargs):
        """Decorator: register a click group."""
        def decorator(func):
            grp = click.group(*args, **kwargs)(func)
            self.commands[grp.name] = grp
            return grp
        return decorator

    async def execute(self, line: str) -> str | None:
        """Parse and execute a command line.

        Returns help/error text, or ``None`` on success.
        """
        tokens = shlex_split(line)
        if not tokens:
            raise ValueError("No command provided")

        cmd_name, *cmd_args = tokens
        if cmd_name not in self.commands:
            raise KeyError(f"Unknown command: /{cmd_name}")

        cmd = self.commands[cmd_name]

        # Intercept --help/-h before click tries to print to stdout
        # TODO: This approach doesn't work correctly for groups with
        # subcommands (e.g. `/options get --help` would show the group
        # help, not the subcommand help). This will be addressed separately.
        if "--help" in cmd_args or "-h" in cmd_args:
            with cmd.make_context(cmd_name, [], max_content_width=self.max_content_width) as ctx:
                return ctx.get_help()

        try:
            result = cmd(cmd_args, standalone_mode=False)
            if inspect.iscoroutine(result):
                result = await result
            return result
        except click.UsageError as e:
            with cmd.make_context(cmd_name, [], max_content_width=self.max_content_width) as ctx:
                return f"{e.format_message()}\n\n{ctx.get_help()}"
