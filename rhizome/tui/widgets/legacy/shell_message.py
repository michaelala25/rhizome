"""Shell command message widget with subprocess execution and streamed output."""

from __future__ import annotations

import asyncio
import time

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static

from rhizome.tui.colors import Colors


class ShellCommandMessage(Widget):
    """Displays a shell command with streamed output in a scrollable sub-area."""

    SHELL_TIMEOUT = 30

    DEFAULT_CSS = f"""
    ShellCommandMessage {{
        height: auto;
        padding: 1 2 1 2;
        background: {Colors.USER_BG};
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
        color: {Colors.SYSTEM_ERROR};
    }}
    """

    def __init__(self, command: str) -> None:
        super().__init__()
        self._command = command

    def compose(self) -> ComposeResult:
        prefix = f"[bold {Colors.USER_PREFIX}]you:[/bold {Colors.USER_PREFIX}] "
        yield Static(f"{prefix}$ {self._command}", classes="shell-header")
        yield Static("", classes="shell-elapsed")
        with VerticalScroll(classes="shell-output-area"):
            yield Static("", classes="shell-output")
        yield Static("", classes="shell-exit-code")

    async def execute(self) -> None:
        """Run the shell command and stream output into the widget."""
        start = time.monotonic()
        elapsed_widget = self.query_one(".shell-elapsed", Static)
        output_area = self.query_one(".shell-output-area", VerticalScroll)
        output_widget = self.query_one(".shell-output", Static)
        exit_code_widget = self.query_one(".shell-exit-code", Static)

        output_area.add_class("--visible")

        elapsed_timer: Timer = self.set_interval(
            1.0, lambda: self._tick_elapsed(start, elapsed_widget)
        )

        lines: list[str] = []
        returncode = -1

        try:
            proc = await asyncio.create_subprocess_shell(
                self._command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
            )

            assert proc.stdout is not None

            try:
                async with asyncio.timeout(self.SHELL_TIMEOUT):
                    async for raw_line in proc.stdout:
                        line = raw_line.decode("utf-8", errors="replace")
                        lines.append(line)
                        output_widget.update("".join(lines).rstrip("\n"))
                        output_area.scroll_end(animate=False)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                lines.append(f"\n[timed out after {self.SHELL_TIMEOUT}s]\n")
                output_widget.update("".join(lines).rstrip("\n"))

            returncode = (
                proc.returncode
                if proc.returncode is not None
                else await proc.wait()
            )

        except Exception as exc:
            output_widget.update(str(exc))
            lines = [str(exc)]
        finally:
            elapsed_timer.stop()

        # Final elapsed display (only if >= 10s)
        total = time.monotonic() - start
        if total >= 10:
            elapsed_widget.update(f"completed in {total:.1f}s")
            elapsed_widget.add_class("--visible")
        else:
            elapsed_widget.remove_class("--visible")

        # Non-zero exit code
        if returncode != 0:
            exit_code_widget.update(f"exit code {returncode}")
            exit_code_widget.add_class("--visible", "--error")

        # Hide output area if nothing was produced
        if not any(line.strip() for line in lines):
            output_area.remove_class("--visible")

    def _tick_elapsed(self, start: float, widget: Static) -> None:
        """Update the elapsed timer display (shown after 10s)."""
        elapsed = time.monotonic() - start
        if elapsed >= 10:
            widget.update(f"running\u2026 ({elapsed:.0f}s)")
            widget.add_class("--visible")
