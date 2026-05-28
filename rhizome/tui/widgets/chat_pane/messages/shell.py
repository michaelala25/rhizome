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


from rhizome.tui.widgets.view_base import ViewBase
from rhizome.app.chat_pane.messages.shell import ShellCommandVM


SHELL_TIMEOUT = 30


class ShellCommandMessage(ViewBase[ShellCommandVM]):
    """Renders a ``ShellCommandVM``: header line, output area, elapsed display (while running and on
    completion if >=10s), and a trailing exit-code line on non-zero exits.

    Uses ``set_interval`` to repaint elapsed while the VM is running; the interval is cancelled once the VM
    flips to finished.
    """

    DEFAULT_CSS = f"""
    ShellCommandMessage {{
        height: auto;
        padding: 1 2 1 2;
        background: rgb(22, 22, 22);
        margin: 0 2;
    }}
    ShellCommandMessage .shell-header {{
        height: auto;
        width: 1fr;
    }}
    ShellCommandMessage .shell-elapsed {{
        height: auto;
        color: $text-muted;
        display: none;
    }}
    ShellCommandMessage .shell-elapsed.--visible {{
        display: block;
    }}
    ShellCommandMessage .shell-output-area {{
        height: auto;
        max-height: 20;
        background: rgb(8, 8, 8);
        margin: 1 0 0 0;
        padding: 0 1;
        display: none;
    }}
    ShellCommandMessage .shell-output-area.--visible {{
        display: block;
    }}
    ShellCommandMessage .shell-output {{
        height: auto;
        width: 1fr;
        color: rgb(180, 180, 180);
    }}
    ShellCommandMessage .shell-exit-code {{
        height: auto;
        color: $text-muted;
        display: none;
    }}
    ShellCommandMessage .shell-exit-code.--visible {{
        display: block;
    }}
    ShellCommandMessage .shell-exit-code.--error {{
        color: rgb(220, 80, 80);
    }}
    """

    def __init__(self, vm: ShellCommandVM, **kwargs) -> None:
        super().__init__(vm, **kwargs)
        self._tick_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        prefix = f"[bold rgb(100, 160, 230)]you:[/bold rgb(100, 160, 230)] "
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

        # Hide the output area only after completion produced nothing visible; during run we want it visible
        # so output appears immediately.
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
