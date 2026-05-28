"""Shell command — sub-VM + view used by the MVVM chat pane.

Buffer entries starting with ``!`` are dispatched as shell commands by the pane: a ``ShellCommandVM``
is appended to the feed and its ``execute()`` coroutine is scheduled on the pane's worker. The VM owns the
subprocess lifecycle, the streamed output, and the final exit code; the view subscribes to ``dirty`` and
renders header / output area / exit code into stock ``Static`` widgets.

The view runs its own ``set_interval`` while the VM is still ``running`` so the "running… (Ns)" elapsed
display updates without the VM having to self-tick. The interval is cancelled on the next ``dirty`` that
arrives after ``running`` flips to False.
"""

from __future__ import annotations

import asyncio
import time

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.timer import Timer
from textual.widgets import Static


from rhizome.app.vm import ViewModelBase


SHELL_TIMEOUT = 30


class ShellCommandVM(ViewModelBase):
    """State for one shell-command invocation.

    Lifecycle:
      - construct with the command string
      - caller schedules ``execute()`` on a worker (e.g. the pane's worker scheduler) — the coroutine runs
        the subprocess, appends to ``output`` as bytes arrive, and flips ``running`` False in its finally
        block
      - subscribers see ``dirty`` on each output append, on completion, and on error
    """

    def __init__(self, command: str) -> None:
        super().__init__()
        self.command: str = command

        # Streamed stdout/stderr lines (raw — view joins them).
        self.output: list[str] = []

        # None while running; int after subprocess exit; -1 if we never got a return code (exception path).
        self.returncode: int | None = None

        # Wall-clock book-keeping. ``finished_at`` is None until ``execute`` returns; the view computes
        # elapsed live while ``running``.
        self.started_at: float | None = None
        self.finished_at: float | None = None

        self.running: bool = False
        self.timed_out: bool = False
        self.error: str | None = None

    # ------------------------------------------------------------------
    # Derived helpers (used by the view)
    # ------------------------------------------------------------------

    def elapsed(self, *, now: float | None = None) -> float:
        if self.started_at is None:
            return 0.0
        end = self.finished_at if self.finished_at is not None else (now or time.monotonic())
        return end - self.started_at

    @property
    def joined_output(self) -> str:
        return "".join(self.output).rstrip("\n")

    @property
    def has_visible_output(self) -> bool:
        return any(line.strip() for line in self.output)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(self) -> None:
        """Run the command via ``asyncio.create_subprocess_shell`` and stream its combined stdout/stderr
        into ``self.output``. Idempotent guard: a second call while already running is a no-op.
        """
        if self.running or self.finished_at is not None:
            return

        self.running = True
        self.started_at = time.monotonic()
        self.emit(self.dirty)

        try:
            proc = await asyncio.create_subprocess_shell(
                self.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
            )
            assert proc.stdout is not None

            try:
                async with asyncio.timeout(SHELL_TIMEOUT):
                    async for raw in proc.stdout:
                        self.output.append(raw.decode("utf-8", errors="replace"))
                        self.emit(self.dirty)
            except TimeoutError:
                self.timed_out = True
                proc.kill()
                await proc.wait()
                self.output.append(f"\n[timed out after {SHELL_TIMEOUT}s]\n")

            self.returncode = proc.returncode if proc.returncode is not None else await proc.wait()

        except Exception as exc:  # noqa: BLE001 — surface subprocess errors on the VM
            self.error = str(exc)
            self.returncode = -1

        finally:
            self.finished_at = time.monotonic()
            self.running = False
            self.emit(self.dirty)
