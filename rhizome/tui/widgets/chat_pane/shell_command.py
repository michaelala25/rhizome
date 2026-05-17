"""Shell command — sub-VM + view used by the MVVM chat pane.

Buffer entries starting with ``!`` are dispatched as shell commands by
the pane: a ``ShellCommandViewModel`` is appended to the feed and its
``execute()`` coroutine is scheduled on the pane's worker. The VM owns
the subprocess lifecycle, the streamed output, and the final exit code;
the view subscribes to ``dirty`` and renders header / output area /
exit code into stock ``Static`` widgets.

The view runs its own ``set_interval`` while the VM is still ``running``
so the "running… (Ns)" elapsed display updates without the VM having to
self-tick. The interval is cancelled on the next ``dirty`` that arrives
after ``running`` flips to False.
"""

from __future__ import annotations

import asyncio
import time

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.timer import Timer
from textual.widgets import Static

from rhizome.tui.colors import Colors

from ..view_base import ViewBase
from ..view_model_base import ViewModelBase


SHELL_TIMEOUT = 30


class ShellCommandViewModel(ViewModelBase):
    """State for one shell-command invocation.

    Lifecycle:
      - construct with the command string
      - caller schedules ``execute()`` on a worker (e.g. the pane's worker
        scheduler) — the coroutine runs the subprocess, appends to
        ``output`` as bytes arrive, and flips ``running`` False in its
        finally block
      - subscribers see ``dirty`` on each output append, on completion,
        and on error
    """

    def __init__(self, command: str) -> None:
        super().__init__()
        self.command: str = command

        # Streamed stdout/stderr lines (raw — view joins them).
        self.output: list[str] = []

        # None while running; int after subprocess exit; -1 if we never
        # got a return code (exception path).
        self.returncode: int | None = None

        # Wall-clock book-keeping. ``finished_at`` is None until ``execute``
        # returns; the view computes elapsed live while ``running``.
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
        """Run the command via ``asyncio.create_subprocess_shell`` and stream
        its combined stdout/stderr into ``self.output``. Idempotent guard:
        a second call while already running is a no-op.
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

            self.returncode = (
                proc.returncode if proc.returncode is not None else await proc.wait()
            )

        except Exception as exc:  # noqa: BLE001 — surface subprocess errors on the VM
            self.error = str(exc)
            self.returncode = -1

        finally:
            self.finished_at = time.monotonic()
            self.running = False
            self.emit(self.dirty)


class ShellCommandView(ViewBase[ShellCommandViewModel]):
    """Renders a ``ShellCommandViewModel``: header line, output area,
    elapsed display (while running and on completion if >=10s), and a
    trailing exit-code line on non-zero exits.

    Uses ``set_interval`` to repaint elapsed while the VM is running;
    the interval is cancelled once the VM flips to finished.
    """

    DEFAULT_CSS = f"""
    ShellCommandView {{
        height: auto;
        padding: 1 2 1 2;
        background: {Colors.USER_BG};
        margin: 0 2;
    }}
    ShellCommandView .shell-header {{
        height: auto;
        width: 1fr;
    }}
    ShellCommandView .shell-elapsed {{
        height: auto;
        color: $text-muted;
        display: none;
    }}
    ShellCommandView .shell-elapsed.--visible {{
        display: block;
    }}
    ShellCommandView .shell-output-area {{
        height: auto;
        max-height: 20;
        background: rgb(8, 8, 8);
        margin: 1 0 0 0;
        padding: 0 1;
        display: none;
    }}
    ShellCommandView .shell-output-area.--visible {{
        display: block;
    }}
    ShellCommandView .shell-output {{
        height: auto;
        width: 1fr;
        color: rgb(180, 180, 180);
    }}
    ShellCommandView .shell-exit-code {{
        height: auto;
        color: $text-muted;
        display: none;
    }}
    ShellCommandView .shell-exit-code.--visible {{
        display: block;
    }}
    ShellCommandView .shell-exit-code.--error {{
        color: {Colors.SYSTEM_ERROR};
    }}
    """

    def __init__(self, vm: ShellCommandViewModel, **kwargs) -> None:
        super().__init__(vm, **kwargs)
        self._tick_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        prefix = f"[bold {Colors.USER_PREFIX}]you:[/bold {Colors.USER_PREFIX}] "
        yield Static(f"{prefix}$ {self._vm.command}", classes="shell-header")
        yield Static("", classes="shell-elapsed")
        with VerticalScroll(classes="shell-output-area"):
            yield Static("", classes="shell-output")
        yield Static("", classes="shell-exit-code")

    def on_mount(self) -> None:
        self._refresh()

    def on_unmount(self) -> None:
        super().on_unmount()
        self._stop_tick()

    # ------------------------------------------------------------------
    # VM → view
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        # Manage the elapsed-tick interval based on running state.
        if self._vm.running and self._tick_timer is None:
            self._tick_timer = self.set_interval(1.0, self._refresh)
        elif not self._vm.running:
            self._stop_tick()

        self._refresh_output()
        self._refresh_elapsed()
        self._refresh_exit_code()

    def _stop_tick(self) -> None:
        if self._tick_timer is not None:
            self._tick_timer.stop()
            self._tick_timer = None

    def _refresh_output(self) -> None:
        area = self.query_one(".shell-output-area", VerticalScroll)
        widget = self.query_one(".shell-output", Static)

        # Hide the output area only after completion produced nothing visible;
        # during run we want it visible so output appears immediately.
        if self._vm.running or self._vm.has_visible_output:
            area.add_class("--visible")
        else:
            area.remove_class("--visible")

        widget.update(self._vm.joined_output if not self._vm.error else self._vm.error)
        area.scroll_end(animate=False)

    def _refresh_elapsed(self) -> None:
        widget = self.query_one(".shell-elapsed", Static)
        elapsed = self._vm.elapsed()

        if self._vm.running:
            if elapsed >= 10:
                widget.update(f"running… ({elapsed:.0f}s)")
                widget.add_class("--visible")
            else:
                widget.remove_class("--visible")
        else:
            if elapsed >= 10:
                widget.update(f"completed in {elapsed:.1f}s")
                widget.add_class("--visible")
            else:
                widget.remove_class("--visible")

    def _refresh_exit_code(self) -> None:
        widget = self.query_one(".shell-exit-code", Static)
        rc = self._vm.returncode
        if not self._vm.running and rc is not None and rc != 0:
            widget.update(f"exit code {rc}")
            widget.add_class("--visible", "--error")
        else:
            widget.remove_class("--visible", "--error")
