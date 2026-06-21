"""Low-level terminal I/O: the one raw-mode exchange, the ioctl, ssh/tmux awareness.

Exactly ONE function here puts the terminal into raw mode — ``exchange`` — and everything that needs to
hear the terminal talk back batches its queries through a single call to it (see ``probe``). So the
library makes a *single* raw-stdin excursion at startup, not one per question: less chance of a stray
keystroke landing mid-read, one timeout instead of several, one place to reason about.

Works only *before* the UI framework starts: Textual spawns a thread that reads stdin for key/mouse
events and would grab the terminal's replies first. After that the channel is gone — the live cell-size
path rides Textual's resize events instead (see ``cellsize`` / ``environment.note_resize``).
"""

import os
import sys
import termios
import tty
from array import array
from fcntl import ioctl
from select import select

_IN_TMUX = bool(os.environ.get("TMUX"))


def is_terminal() -> bool:
    """Whether both stdin and stdout are real terminals — the precondition for any exchange."""
    return bool(sys.__stdin__ and sys.__stdout__ and sys.__stdin__.isatty() and sys.__stdout__.isatty())


def over_ssh() -> bool:
    """Whether this process is running over an ssh session (an upfront, env-based signal).

    Used to *distrust the ioctl's pixel fields*: over ssh they are whatever the client forwarded, which
    can be a fixed lie (Windows OpenSSH → WSL sshd sends a hardcoded 640x480). See ``cellsize``.
    """
    return bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"))


def tiocgwinsz() -> tuple[int, int, int, int] | None:
    """The kernel pty's window size ``(rows, cols, xpixel, ypixel)``, or None if unavailable.

    A plain syscall — no raw mode, no round-trip, no terminal disturbance. rows/cols are reliable;
    ``xpixel``/``ypixel`` are *optional* fields the emulator may leave at 0, and over ssh may be a lie.
    """
    if not (sys.__stdout__ and sys.__stdout__.isatty()):
        return None
    try:
        buf = array("H", [0, 0, 0, 0])
        ioctl(sys.__stdout__, termios.TIOCGWINSZ, buf)
        rows, cols, xpixel, ypixel = buf
        return int(rows), int(cols), int(xpixel), int(ypixel)
    except OSError:
        return None


def _tmux_wrap(sequence: str) -> str:
    """Wrap a sequence in tmux's passthrough envelope so it reaches the real terminal underneath.

    Inside tmux, escapes tmux doesn't recognize are swallowed unless wrapped as ``ESC P tmux; … ESC \\``
    with every embedded ESC doubled. Best-effort: reply routing back through tmux is unreliable for some
    queries, and tmux sixel itself needs tmux >= 3.4 built ``--enable-sixel``.
    """
    if not _IN_TMUX:
        return sequence
    return "\x1bPtmux;" + sequence.replace("\x1b", "\x1b\x1b") + "\x1b\\"


def exchange(payload: str, *, end: str, timeout: float = 0.5) -> str | None:
    """The library's only raw-mode excursion: write ``payload``, read until ``end`` or a silent ``timeout``.

    Returns the accumulated reply (possibly partial, if the terminal went quiet before ``end`` arrived),
    or None if stdin/stdout isn't a terminal. The terminal mode is always restored in a ``finally``, so a
    raised exception or a hung terminal can't leave the user's shell in raw mode.

    Callers batch *all* their queries into one ``payload`` and pass the terminator of the last reply as
    ``end`` — so this runs once, at startup (see ``probe.probe_terminal``). Reading is best-effort: a
    keystroke arriving mid-read can corrupt the buffer, accepted because this only runs pre-Textual.
    """
    if not is_terminal():
        return None
    fd = sys.__stdin__.buffer.fileno()
    original = termios.tcgetattr(fd)
    buf = ""
    try:
        tty.setcbreak(fd, termios.TCSANOW)
        sys.__stdout__.write(_tmux_wrap(payload))
        sys.__stdout__.flush()
        while not buf.endswith(end):
            ready, _, _ = select([fd], [], [], timeout)
            if not ready:                              # the terminal went quiet — stop with what we have
                break
            buf += os.read(fd, 64).decode("latin-1")
    except OSError:
        return None
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, original)
    return buf
