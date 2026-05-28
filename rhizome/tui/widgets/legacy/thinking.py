"""Animated spinner widget with configurable label."""

from textual.widgets import Static

_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class Spinner(Static):
    """A braille-spinner with a configurable label."""

    DEFAULT_CSS = """
    Spinner {
        height: 1;
        margin-top: 1;
        margin-bottom: 1;
        padding: 0 0 0 4;
        color: $text-muted;
    }
    """

    def __init__(self, label: str = "", *, tick_rate: float = 0.1) -> None:
        super().__init__(f"{_FRAMES[0]} {label}")
        self._label = label
        self._tick_rate = tick_rate
        self._frame = 0

    def on_mount(self) -> None:
        self.set_interval(self._tick_rate, self._tick)

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(_FRAMES)
        self.content = f"{_FRAMES[self._frame]} {self._label}"


class ThinkingIndicator(Spinner):
    """A spinner with the label 'thinking...'."""

    def __init__(self) -> None:
        super().__init__("thinking...")
