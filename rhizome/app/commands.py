"""Slash-command parsing and a scoped command registry.

Commands are simple enough that we parse them by hand rather than lean on a CLI framework. The grammar
is ``/<name> <rest>``: the first whitespace-delimited token (minus its leading ``/``) names the command,
and *everything after it* is the raw remainder. How that remainder turns into handler arguments is the
job of the command's :class:`CommandParser` -- the per-command seam that keeps the registry core free of
flag-grammar machinery.

    Parser            Remainder handling                        Used by
    ----------------  ----------------------------------------  -----------------------------------
    NULLARY (default) ignored                                   /clear, /quit, /idle, ...
    RAW               passed untouched as one string            /rename, /branch, /echo (no shlex!)
    DefaultParser     peel declared flags, rejoin the rest      /options --global, /commit --auto

``RAW`` never tokenizes, so quotes and apostrophes survive intact (``/rename Can't Stop`` just works) --
the reason there is no special-case interception of prompt-bearing commands upstream.

Scope mirrors :class:`~rhizome.app.options.OptionService`: a registry holds a ``parent`` link, child
scopes shadow parents by command name, and ``rows`` / ``all_commands`` return the merged view. A global
registry lives at the app-root service scope (``/quit``, ``/new``, ``/logs``); a session registry parented
to it lives at each conversation scope (everything else). Dispatch resolves the session registry and falls
through to the global one for names it doesn't own.

``/help`` and ``--help`` are built into the registry core -- they read the merged command set, so every
scope gets them for free.
"""

from __future__ import annotations

import inspect
import keyword
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, Sequence


# ========================================================================================================================
# Parsed arguments + the parser protocol
# ========================================================================================================================


@dataclass(frozen=True)
class ParsedArgs:
    """A parser's output: positional ``args`` and keyword ``kwargs`` for the command handler."""

    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)


class CommandParser(ABC):
    """Turns the raw remainder of a command line into handler arguments, and describes its own usage.

    ``usage`` keeps help text co-located with the grammar that produced it, so ``/help`` never has to
    reverse-engineer a parser to explain a command."""

    @abstractmethod
    def parse(self, raw: str) -> ParsedArgs: ...

    @abstractmethod
    def usage(self, name: str) -> str: ...


class _Nullary(CommandParser):
    """No arguments; the remainder is ignored."""

    def parse(self, raw: str) -> ParsedArgs:
        return ParsedArgs()

    def usage(self, name: str) -> str:
        return f"/{name}"


class _Raw(CommandParser):
    """The entire remainder, untokenized, as one stripped string. Quotes/apostrophes survive."""

    def parse(self, raw: str) -> ParsedArgs:
        return ParsedArgs(args=(raw.strip(),))

    def usage(self, name: str) -> str:
        return f"/{name} <text>"


NULLARY: CommandParser = _Nullary()
RAW: CommandParser = _Raw()


# ========================================================================================================================
# DefaultParser -- the dumbed-down flag parser
# ========================================================================================================================


@dataclass(frozen=True)
class Flag:
    """A boolean flag for :class:`DefaultParser`. ``name`` is the long form (``--name``); ``dest`` is the
    handler kwarg it binds (defaults to a Python-safe form of ``name``)."""

    name: str
    short: str | None = None
    help: str = ""
    dest: str | None = None

    def kwarg(self) -> str:
        if self.dest is not None:
            return self.dest
        ident = self.name.replace("-", "_")
        return f"{ident}_" if keyword.iskeyword(ident) else ident


class DefaultParser(CommandParser):
    """A small, shlex-free parser: peel declared boolean flags off the front, then hand the rejoined
    leftover to the handler as a single raw ``rest`` positional (omitted when ``rest`` is False).

    Flags are matched by their long (``--name``) or short (``-x``) form. Unknown ``-``/``--`` tokens
    raise :class:`CommandUsageError`; everything else is treated as the start of ``rest``."""

    def __init__(self, *, flags: Sequence[Flag] = (), rest: bool = False) -> None:
        self._flags = tuple(flags)
        self._rest = rest
        self._by_token: dict[str, Flag] = {}
        for flag in self._flags:
            self._by_token[f"--{flag.name}"] = flag
            if flag.short:
                self._by_token[flag.short] = flag

    def parse(self, raw: str) -> ParsedArgs:
        kwargs: dict[str, Any] = {flag.kwarg(): False for flag in self._flags}
        tokens = raw.split()
        i = 0
        while i < len(tokens) and tokens[i].startswith("-"):
            flag = self._by_token.get(tokens[i])
            if flag is None:
                raise CommandUsageError(f"Unknown option: {tokens[i]}")
            kwargs[flag.kwarg()] = True
            i += 1

        rest = " ".join(tokens[i:])
        if not self._rest and rest:
            raise CommandUsageError(f"Unexpected argument: {rest!r}")
        args = (rest,) if self._rest else ()
        return ParsedArgs(args=args, kwargs=kwargs)

    def usage(self, name: str) -> str:
        parts = [f"/{name}"]
        parts += [f"[--{flag.name}]" for flag in self._flags]
        if self._rest:
            parts.append("[<text>]")
        return " ".join(parts)

    @property
    def flags(self) -> tuple[Flag, ...]:
        return self._flags


# ========================================================================================================================
# Command + errors
# ========================================================================================================================


@dataclass
class Command:
    """A single slash command: its name, one-line help, handler, and the parser that feeds it."""

    name: str
    handler: Callable[..., Any | Awaitable[Any]]
    help: str = ""
    parser: CommandParser = NULLARY

    def usage(self) -> str:
        return self.parser.usage(self.name)


class CommandError(Exception):
    """Base for command resolution/parsing failures surfaced back to the user as messages."""


class UnknownCommandError(CommandError, KeyError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"Unknown command: /{name}")

    def __str__(self) -> str:  # KeyError stringifies with surrounding quotes; we want the plain message
        return self.args[0]


class CommandUsageError(CommandError):
    """A command was found but its arguments didn't parse."""


# ========================================================================================================================
# Service: CommandRegistryService
#   Shape : protocol + first-party impl (CommandRegistry, below)
#   Scope : root -> workspace -> conversation (scoped; child registries merge with their parent)
# ========================================================================================================================


class CommandRegistryService(Protocol):
    """The consumer-facing slice of a command registry: dispatch a line, and read the merged command set.

    Consumers (the conversation's dispatch, the command palette) depend on this protocol rather than the
    concrete :class:`CommandRegistry`, so the dependency reads as an injected service. The palette pulls
    ``rows`` lazily, so commands registered after construction still appear."""

    def resolve(self, name: str) -> Command | None: ...
    def all_commands(self) -> dict[str, Command]: ...
    def rows(self) -> list[tuple[str, str]]: ...
    async def execute(self, line: str) -> str | None: ...


class CommandRegistry(CommandRegistryService):
    """A scoped slash-command registry. Children created with ``parent=`` shadow the parent by command
    name and merge for the read paths (``all_commands`` / ``rows``); dispatch falls through to the parent
    for names this scope doesn't own. ``Options`` is the model for the shape.

    ``/help`` (overview or per-command) and a trailing ``--help`` are handled in ``execute`` from the
    merged set, so no scope has to register them."""

    def __init__(self, parent: CommandRegistry | None = None) -> None:
        self._commands: dict[str, Command] = {}
        self._parent = parent

    # ----- registration ---------------------------------------------------- #

    def register(
        self, name: str, handler: Callable[..., Any], *, help: str = "", parser: CommandParser = NULLARY
    ) -> Command:
        """Register ``/name`` with ``handler``. The primary, explicit registration API."""
        return self.add(Command(name=name, handler=handler, help=help, parser=parser))

    def add(self, command: Command) -> Command:
        """Register a prebuilt :class:`Command`."""
        if command.name in self._commands:
            raise ValueError(f"Command /{command.name} is already registered in this scope.")
        self._commands[command.name] = command
        return command

    # ----- read paths (merged with parents) -------------------------------- #

    def resolve(self, name: str) -> Command | None:
        cmd = self._commands.get(name)
        if cmd is not None:
            return cmd
        return self._parent.resolve(name) if self._parent is not None else None

    def all_commands(self) -> dict[str, Command]:
        merged: dict[str, Command] = {}
        if self._parent is not None:
            merged.update(self._parent.all_commands())
        merged.update(self._commands)  # this scope shadows parents by name
        return merged

    def rows(self) -> list[tuple[str, str]]:
        """``(name, one-line help)`` for every reachable command, sorted by name."""
        rows = [(cmd.name, _first_line(cmd.help)) for cmd in self.all_commands().values()]
        return sorted(rows, key=lambda r: r[0])

    # ----- dispatch -------------------------------------------------------- #

    async def execute(self, line: str) -> str | None:
        """Parse and run a command line (with or without the leading ``/``). Returns help/echo text to
        surface as a system message, or ``None`` when the handler has nothing to say."""
        name, raw = _split_line(line)
        if not name:
            raise UnknownCommandError("")

        if name == "help":
            return self._help(raw.strip())

        cmd = self.resolve(name)
        if cmd is None:
            raise UnknownCommandError(name)

        if raw.strip() in ("--help", "-h"):
            return self._help_for(cmd)

        parsed = cmd.parser.parse(raw)
        result = cmd.handler(*parsed.args, **parsed.kwargs)
        if inspect.iscoroutine(result):
            result = await result
        return result

    # ----- help ------------------------------------------------------------ #

    def _help(self, target: str) -> str:
        if target:
            name = target.lstrip("/")
            cmd = self.resolve(name)
            if cmd is None:
                return f"Unknown command: /{name}\nType /help to see available commands."
            return self._help_for(cmd)

        lines = ["**Available commands:**", ""]
        for name, desc in self.rows():
            lines.append(f"  /{name} — {desc}" if desc else f"  /{name}")
        return "\n".join(lines)

    def _help_for(self, cmd: Command) -> str:
        lines = [f"**/{cmd.name}** — {cmd.help}" if cmd.help else f"**/{cmd.name}**", ""]
        lines.append(f"Usage: {cmd.usage()}")
        parser = cmd.parser
        if isinstance(parser, DefaultParser) and parser.flags:
            lines.append("")
            for flag in parser.flags:
                token = f"--{flag.name}" + (f", {flag.short}" if flag.short else "")
                lines.append(f"  {token}" + (f" — {flag.help}" if flag.help else ""))
        return "\n".join(lines)


# ========================================================================================================================
# helpers
# ========================================================================================================================


def _split_line(line: str) -> tuple[str, str]:
    """Split ``/name rest`` into ``(name, rest)``. Only the first token is consumed; ``rest`` is raw."""
    stripped = line.strip().lstrip("/")
    name, _, raw = stripped.partition(" ")
    return name, raw


def _first_line(text: str) -> str:
    text = (text or "").strip()
    return text.splitlines()[0] if text else ""
