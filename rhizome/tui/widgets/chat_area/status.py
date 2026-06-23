"""StatusBar view — renders mode, verbosity, model, and live token usage from a ``StatusBarModel``.

A strip docked at the bottom of the chat area. Two lines until the branch has a usage report, three once
it does:

- line 1 — active mode (left), model name + the active provider's knobs (right)
- line 2 — answer verbosity (left), context-window fill (right)
- line 3 — the prompt's token breakdown by category (left), cache TTL + read/write split (right)

Every value is a projection the VM owns (``StatusBarModel``); the view paints it and repaints on the VM's
``OnDirty``. The usage report (``UsageReport``) carries the provider's ground-truth totals plus a
normalized per-segment estimate; this view rolls the engine's fine-grained segment kinds up into a handful
of display buckets — see ``_BUCKETS``.
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from rhizome.agent.engine import UsageReport
from rhizome.app.chat_area.status import StatusBarModel
from rhizome.tui.widgets.view_base import ViewBase


_LABEL = "rgb(140,140,140)"
_MODEL = "rgb(90,90,90)"
_DIM = "rgb(100,100,100)"

_MODE_COLORS: dict[str, str] = {
    "learn": "rgb(110,140,240)",
    "review": "rgb(170,90,220)",
}

_EFFORT_COLORS: dict[str, str] = {
    "low": "rgb(120,120,120)",
    "medium": "rgb(220,160,80)",
    "high": "rgb(90,210,190)",
    "max": "rgb(255,80,255)",
}

_VERBOSITY_COLORS: dict[str, str] = {
    "terse": "rgb(120,120,120)",
    "standard": "rgb(255,255,255)",
    "verbose": "rgb(90,210,190)",
    "auto": "rgb(255,80,255)",
}

# The prompt breakdown the bar shows, ordered fixed-overhead-first then by what grows as you work: the
# fixed framing (system prompt + tool-definition schemas + guides/markers — a constant the user can't act
# on, so the schemas fold in here rather than earning their own line), then stuffed resource context, then
# live tool round-trips, then the plain conversation turns. Each bucket rolls up several of the engine's
# segment kinds (see ``PromptEngine._message_kind`` / ``RootPromptEngine._message_kind``); any kind not
# claimed here lands in a trailing "other" bucket.
_BUCKETS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("system",    "rgb(120,120,120)", ("system", "tools", "system_notice", "guide", "branch_marker")),
    ("resources", "rgb(110,140,240)", ("global_resource", "local_resource", "resource_index")),
    ("tools",     "rgb(220,160,80)",  ("tool_use", "tool_result")),
    ("chat",      "rgb(140,190,140)", ("user", "agent")),
)
_OTHER = ("other", _DIM)

# Display order (bucket label -> color) and the inverse kind -> bucket map, both built once at import.
_DISPLAY: tuple[tuple[str, str], ...] = tuple((label, color) for label, color, _ in _BUCKETS) + (_OTHER,)
_KIND_TO_BUCKET: dict[str, str] = {kind: label for label, _, kinds in _BUCKETS for kind in kinds}


def _fmt(n: int) -> str:
    """Compact token count: ``8.4k`` past a thousand, the bare number below it."""
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _context_color(pct: float) -> str:
    """Calm under half-full, amber as the window fills, red near the ceiling."""
    if pct >= 85:
        return "rgb(230,90,90)"
    if pct >= 60:
        return "rgb(220,160,80)"
    return "rgb(120,180,120)"


class StatusBar(ViewBase[StatusBarModel]):

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
        # Right-alignment of the model name / usage figures depends on the bar's pixel width.
        self._refresh()

    def _refresh(self) -> None:
        if self._static is not None:
            self._static.update(self._build_text())

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------

    def _right_align(self, left: Text, right: Text) -> Text:
        gap = max(self.size.width - len(left.plain) - len(right.plain), 2)
        left.append(" " * gap)
        left.append(right)
        return left

    def _build_text(self) -> Text:
        vm = self._vm

        # -- line 1: mode (left), model name (right) --
        mode_line = Text()
        mode_line.append("mode: ", style=_LABEL)
        mode_line.append(vm.mode, style=_MODE_COLORS.get(vm.mode, ""))

        model_text = Text()
        if vm.model_name:
            model_text.append(vm.model_name, style=_MODEL)
            self._append_provider_knobs(model_text, vm)
        self._right_align(mode_line, model_text)

        # -- line 2: verbosity (left), context-window fill (right) --
        verbosity_line = Text()
        verbosity_line.append("verbosity: ", style=_LABEL)
        verbosity_line.append(vm.verbosity, style=_VERBOSITY_COLORS.get(vm.verbosity, ""))
        self._right_align(verbosity_line, self._context_text(vm.usage_report))

        lines: list[Text] = [mode_line, verbosity_line]

        # -- line 3: prompt breakdown (left), cache split (right) -- only once a model call has landed --
        report = vm.usage_report
        if report is not None and report.usage is not None:
            breakdown_line = self._breakdown_text(report)
            self._right_align(breakdown_line, self._cache_text(report, vm.prompt_cache_ttl))
            lines.append(breakdown_line)

        parts: list[Text | str] = []
        for i, line in enumerate(lines):
            if i:
                parts.append("\n")
            parts.append(line)
        return Text.assemble(*parts)

    def _append_provider_knobs(self, model_text: Text, vm: StatusBarModel) -> None:
        """Append the active provider's knobs after the model name as ``(thinking · high)``. Each reads as
        ``None`` when it doesn't apply to the current provider, in which case its segment is dropped; if
        none apply, no parens are drawn at all."""
        knobs: list[tuple[str, str]] = []
        if vm.adaptive_thinking is not None:
            knobs.append(("thinking", "rgb(90,210,190)") if vm.adaptive_thinking else ("no thinking", _DIM))
        # Effort only matters while thinking is on — hide it once thinking is off.
        if vm.effort is not None and vm.adaptive_thinking:
            knobs.append((vm.effort, _EFFORT_COLORS.get(vm.effort, _MODEL)))
        if not knobs:
            return

        model_text.append(" (", style=_DIM)
        for i, (label, color) in enumerate(knobs):
            if i:
                model_text.append(" · ", style=_DIM)
            model_text.append(label, style=color)
        model_text.append(")", style=_DIM)

    def _context_text(self, report: UsageReport | None) -> Text:
        """Right of line 2: how full the context window is. Empty before the thread's first model call;
        falls back to the raw prompt size when the model profile yields no window ceiling."""
        text = Text()
        if report is None or report.usage is None:
            return text

        input_tokens = report.usage.input_tokens
        pct = report.usage_percent
        if pct is not None:
            text.append("context: ", style=_LABEL)
            text.append(f"{pct:.1f}%", style=_context_color(pct))
            text.append(f"  ({input_tokens:,} / {report.max_input_tokens:,})", style=_DIM)
        else:
            text.append("prompt: ", style=_LABEL)
            text.append(f"{input_tokens:,} tokens", style=_MODEL)
        return text

    def _breakdown_text(self, report: UsageReport) -> Text:
        """Left of line 3: the prompt's estimated composition, segment kinds rolled into display buckets and
        shown in prompt order. Buckets with no tokens are dropped."""
        totals: dict[str, int] = {}
        for kind, tokens in report.by_kind().items():
            bucket = _KIND_TO_BUCKET.get(kind, _OTHER[0])
            totals[bucket] = totals.get(bucket, 0) + tokens

        text = Text()
        for label, color in _DISPLAY:
            tokens = totals.get(label, 0)
            if not tokens:
                continue
            if text.plain:
                text.append(" · ", style=_DIM)
            text.append(f"{label} ", style=_DIM)
            text.append(_fmt(tokens), style=color)
        return text

    def _cache_text(self, report: UsageReport, ttl: str | None) -> Text:
        """Right of line 3: the active cache TTL (when caching is on) and the cache read/write split of this
        prompt's input — read is cheap, new is premium. The read/write split is empty when the prompt
        neither hit nor wrote the cache (a cold first call on the thread); the TTL still shows."""
        usage = report.usage
        text = Text()
        if ttl is not None:
            text.append("ttl ", style=_LABEL)
            text.append(ttl, style=_MODEL)
        if usage.cache_read_tokens or usage.cache_creation_tokens:
            if text.plain:
                text.append("   ", style=_DIM)
            text.append("cache: ", style=_LABEL)
            text.append(f"{_fmt(usage.cache_read_tokens)} read", style="rgb(120,180,120)")
            text.append(" · ", style=_DIM)
            text.append(f"{_fmt(usage.cache_creation_tokens)} new", style="rgb(220,160,80)")
        return text
