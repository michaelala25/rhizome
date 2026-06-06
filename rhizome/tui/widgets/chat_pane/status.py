"""Status bar — sub-VM + view used by the MVVM chat pane.

The status bar is a projection of facts that live elsewhere: mode and topic_path on the pane VM,
token_usage + model_name on the AgentSession, verbosity on app.options. Rather than have the view
reach into all three, ``StatusBarModel`` owns the projected slice. Each source's update path
writes through to a setter here; the setter no-ops on no change and emits ``dirty`` otherwise —
giving the bar repaint isolation from the rest of the pane's dirty churn (token usage in particular
updates on every model chunk).

The view ports the legacy ``widgets/status_bar.py`` render verbatim, sourced from the VM instead of
Textual reactives.
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from rhizome.agent.utils import TokenUsageData

from rhizome.tui.widgets.view_base import ViewBase
from rhizome.app.chat_pane.status import StatusBarModel


def _compact_rgb(s: str) -> str:
    """Strip spaces from RGB strings so Rich can parse them."""
    return s.replace(" ", "")


_MODE_COLORS: dict[str, str] = {
    "learn": _compact_rgb("rgb(110, 140, 240)"),
    "review": _compact_rgb("rgb(170, 90, 220)"),
}

_VERBOSITY_COLORS: dict[str, str] = {
    "terse": "rgb(120,120,120)",
    "standard": "rgb(255,255,255)",
    "verbose": "rgb(90,210,190)",
    "auto": "rgb(255,80,255)",
}

# Max characters for the rendered topic path (excluding the "topic: " prefix).
TOPIC_PATH_MAX = 60


class StatusBar(ViewBase[StatusBarModel]):
    """Renders the status bar from VM state. Three lines:
    line 1: topic path (left), model name (right)
    line 2: mode (left), token usage (right)
    line 3: verbosity (left), cache usage (right)
    """

    DEFAULT_CSS = """
    StatusBar {
        height: auto;
        background: rgb(12, 12, 12);
        padding: 0 1 1 1;
        border-top: solid rgb(60, 60, 60);
    }
    """

    def __init__(self, vm: StatusBarModel, **kwargs) -> None:
        super().__init__(vm, **kwargs)
        self._static: Static | None = None

    def on_mount(self) -> None:
        self._static = Static(self._build_text())
        self.mount(self._static)

    def on_resize(self, event) -> None:
        # Right-alignment depends on the bar's pixel width.
        if self._static is not None:
            self._static.update(self._build_text())

    def _refresh(self) -> None:
        if self._static is not None:
            self._static.update(self._build_text())

    # ------------------------------------------------------------------
    # Render (ported from legacy widgets/status_bar.py)
    # ------------------------------------------------------------------

    def _right_align(self, left: Text, right: Text) -> Text:
        gap = max(self.size.width - len(left.plain) - len(right.plain), 2)
        left.append(" " * gap)
        left.append(right)
        return left

    def _build_text(self) -> Text:
        vm = self._vm
        _label = "rgb(140,140,140)"

        # -- line 1: topic path (left), model name (right) --
        topic_line = Text()
        topic_line.append("topic: ", style=_label)
        if vm.topic_path:
            sep = " > "
            full = sep.join(vm.topic_path)
            if len(full) <= TOPIC_PATH_MAX:
                topic_line.append(full)
            else:
                parts = list(vm.topic_path)
                while len(parts) > 1 and len(sep.join(parts)) + len("... > ") > TOPIC_PATH_MAX:
                    parts.pop(0)
                topic_line.append("... > " + sep.join(parts))
        else:
            topic_line.append("none", style="rgb(100,100,100)")

        model_text = Text()
        if vm.model_name:
            model_text.append(vm.model_name, style="rgb(90,90,90)")
        self._right_align(topic_line, model_text)

        # -- line 2: mode (left), token usage (right) --
        mode_line = Text()
        mode_line.append("mode: ", style=_label)
        mode_color = _MODE_COLORS.get(vm.mode)
        if mode_color:
            mode_line.append(vm.mode, style=mode_color)
        else:
            mode_line.append(vm.mode)
        mode_line.append("  (shift+tab to cycle)", style="rgb(100,100,100)")

        token_text = Text()
        if vm.token_usage.total_tokens:
            total = vm.token_usage.total_tokens

            system_overhead = vm.token_usage.breakdown.get(TokenUsageData.BreakdownCategory.SYSTEM)
            tool_overhead = vm.token_usage.breakdown.get(TokenUsageData.BreakdownCategory.TOOL_MESSAGES)

            if system_overhead is not None or tool_overhead is not None:
                overhead_parts = []
                if system_overhead is not None:
                    overhead_parts.append((f"system: {system_overhead:,}", "rgb(120,120,120)"))
                if tool_overhead is not None:
                    overhead_parts.append((f"tools: {tool_overhead:,}", "rgb(220,160,80)"))

                token_text.append(f"tokens: {total:,}")
                token_text.append(" (", style="rgb(100,100,100)")

                for i, (part, color) in enumerate(overhead_parts):
                    token_text.append(f"{part}", style=color)
                    if i < len(overhead_parts) - 1:
                        token_text.append(", ", style="rgb(100,100,100)")

                token_text.append(")", style="rgb(100,100,100)")
            else:
                token_text.append(f"tokens: {total:,}")

            pct = vm.token_usage.usage_percent
            if pct is not None:
                token_text.append(f"  context usage: {pct:.1f}%")

        self._right_align(mode_line, token_text)

        # -- line 3: verbosity (left), cache usage (right) --
        cache_line = Text()
        cache_line.append("verbosity: ", style=_label)
        verbosity_color = _VERBOSITY_COLORS.get(vm.verbosity)
        cache_line.append(vm.verbosity, style=verbosity_color or "")
        cache_line.append("  (ctrl+b to cycle)", style="rgb(100,100,100)")

        cache_read = vm.token_usage.cache_read_tokens
        cache_create = vm.token_usage.cache_creation_tokens
        if cache_read is not None or cache_create is not None:
            cache_text = Text()
            cache_text.append(f"cache read: {cache_read:,}  create: {cache_create:,}", style="rgb(90,90,90)")
            self._right_align(cache_line, cache_text)

        return Text.assemble(topic_line, "\n", mode_line, "\n", cache_line)