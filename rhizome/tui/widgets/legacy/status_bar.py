"""Persistent status bar showing the active mode and context."""

from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static

from rhizome.agent.utils import TokenUsageData
from rhizome.tui.colors import Colors

def _compact_rgb(s: str) -> str:
    """Strip spaces from RGB strings so Rich can parse them."""
    return s.replace(" ", "")

_MODE_COLORS: dict[str, str] = {
    "learn": _compact_rgb(Colors.LEARN_SYSTEM_TEXT),
    "review": _compact_rgb(Colors.REVIEW_SYSTEM_TEXT),
}

_VERBOSITY_COLORS: dict[str, str] = {
    "terse": "rgb(120,120,120)",
    "standard": "rgb(255,255,255)",
    "verbose": "rgb(90,210,190)",
    "auto": "rgb(255,80,255)",
}


class StatusBar(Static):
    """Displays the current mode and active topic context."""

    mode: reactive[str] = reactive("idle")
    topic_path: reactive[list[str]] = reactive(list)
    token_usage: reactive[TokenUsageData] = reactive(TokenUsageData)
    model_name: reactive[str] = reactive("")
    verbosity: reactive[str] = reactive("auto")

    # Max characters for the rendered topic path (excluding the "topic: " prefix).
    TOPIC_PATH_MAX = 60

    def _right_align(self, left: Text, right: Text) -> Text:
        """Append *right* to *left* with gap-padding to right-align."""
        gap = max(self.size.width - len(left.plain) - len(right.plain), 2)
        left.append(" " * gap)
        left.append(right)
        return left

    def render(self) -> Text:
        _label = "rgb(140,140,140)"

        # -- line 1: topic path (left), model name (right) --
        topic_line = Text()
        topic_line.append("topic: ", style=_label)
        if self.topic_path:
            sep = " > "
            full = sep.join(self.topic_path)
            if len(full) <= self.TOPIC_PATH_MAX:
                topic_line.append(full)
            else:
                parts = list(self.topic_path)
                while len(parts) > 1 and len(sep.join(parts)) + len("... > ") > self.TOPIC_PATH_MAX:
                    parts.pop(0)
                topic_line.append("... > " + sep.join(parts))
        else:
            topic_line.append("none", style="rgb(100,100,100)")

        model_text = Text()
        if self.model_name:
            model_text.append(self.model_name, style="rgb(90,90,90)")
        self._right_align(topic_line, model_text)

        # -- line 2: mode (left), token usage (right) --
        mode_line = Text()
        mode_line.append("mode: ", style=_label)
        mode_color = _MODE_COLORS.get(self.mode)
        if mode_color:
            mode_line.append(self.mode, style=mode_color)
        else:
            mode_line.append(self.mode)
        mode_line.append("  (shift+tab to cycle)", style="rgb(100,100,100)")

        token_text = Text()
        if self.token_usage.total_tokens:
            total = self.token_usage.total_tokens

            system_overhead = self.token_usage.breakdown.get(TokenUsageData.BreakdownCategory.SYSTEM)
            tool_overhead = self.token_usage.breakdown.get(TokenUsageData.BreakdownCategory.TOOL_MESSAGES)

            if system_overhead is not None or tool_overhead is not None:
                overhead_parts = []
                if system_overhead is not None:
                    overhead_parts.append((
                        f"system: {system_overhead:,}",
                        "rgb(120,120,120)"
                    ))
                if tool_overhead is not None:
                    overhead_parts.append((
                        f"tools: {tool_overhead:,}",
                        "rgb(220,160,80)",
                    ))

                token_text.append(f"tokens: {total:,}")
                token_text.append(" (", style="rgb(100,100,100)")

                for i, (part, color) in enumerate(overhead_parts):
                    token_text.append(f"{part}", style=color)
                    if i < len(overhead_parts) - 1:
                        token_text.append(", ", style="rgb(100,100,100)")

                token_text.append(")", style="rgb(100,100,100)")
            else:
                token_text.append(f"tokens: {total:,}")

            pct = self.token_usage.usage_percent
            if pct is not None:
                token_text.append(f"  context usage: {pct:.1f}%")

        self._right_align(mode_line, token_text)

        # -- line 3: verbosity (left), cache usage (right) --
        cache_line = Text()
        cache_line.append("verbosity: ", style=_label)
        verbosity_color = _VERBOSITY_COLORS.get(self.verbosity)
        cache_line.append(self.verbosity, style=verbosity_color or "")
        cache_line.append("  (ctrl+b to cycle)", style="rgb(100,100,100)")

        cache_read = self.token_usage.cache_read_tokens
        cache_create = self.token_usage.cache_creation_tokens
        if cache_read is not None or cache_create is not None:
            cache_text = Text()
            cache_text.append(
                f"cache read: {cache_read:,}  create: {cache_create:,}",
                style="rgb(90,90,90)",
            )
            self._right_align(cache_line, cache_text)

        return Text.assemble(topic_line, "\n", mode_line, "\n", cache_line)
