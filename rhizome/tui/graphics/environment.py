"""The one graphics environment for the process: which backend, why-not, and the live cell size.

A terminal app talks to exactly one terminal, so the selected backend, the failure reason (when there is
none), and the cell-size resolver are process-global state. ``initialize()`` populates them — call it
ONCE, from the app's entry point, BEFORE Textual starts (it runs the one terminal probe, which needs
stdin while it is still ours). Widgets then read this singleton; nothing else has to thread a backend
around.

The startup flow is intentionally shallow — one gate, then one probe, then pure interpretation:

    is_terminal()? ─no─▶ NOT_A_TERMINAL
         │ yes
    probe_terminal()  ── the ONE raw-mode excursion ──▶ TerminalProbe
         ├── cell size = CellMetrics.from_probe(probe)        (never fails; may be low-confidence)
         └── backend   = _select(probe):  sixel? + encoder? ─▶ SixelBackend | NO_PROTOCOL | ENCODER_MISSING

  initialize(backend=...)  injects a backend and skips selection — for tests or to force a protocol (the
                           cell size is still seeded from the terminal).
  note_resize(...)         the live cell-size feed: wire it from the app's ``on_resize`` (see below).
"""

from dataclasses import dataclass

from rhizome.tui.graphics.render.backend import GraphicsBackend
from rhizome.tui.graphics.render.sixel import SixelBackend
from rhizome.tui.graphics.terminal.capabilities import GraphicsUnavailable, encoder_available
from rhizome.tui.graphics.terminal.cellsize import CellMetrics, CellSize
from rhizome.tui.graphics.terminal.probe import TerminalProbe, probe_terminal
from rhizome.tui.graphics.terminal.query import is_terminal


@dataclass
class GraphicsEnvironment:
    """The selected backend (or None + a reason) and the live cell-size resolver."""
    backend: GraphicsBackend | None
    reason: GraphicsUnavailable | None
    metrics: CellMetrics


# Default until initialize() runs: no backend, the reason says so, cell size at the VT340 fallback.
_ENV = GraphicsEnvironment(None, GraphicsUnavailable.NOT_INITIALIZED,
                           CellMetrics(CellSize(10, 20), "uninitialized", confident=False))


def initialize(*, backend: GraphicsBackend | None = None) -> GraphicsEnvironment:
    """Run the one probe, seed the cell size, and select a backend. Call once, before Textual starts.

    ``backend`` overrides selection entirely (DI for tests / forcing a protocol); the cell size is still
    seeded from the terminal so geometry is correct regardless.
    """
    global _ENV
    if not is_terminal():
        _ENV = GraphicsEnvironment(backend, None if backend else GraphicsUnavailable.NOT_A_TERMINAL,
                                   CellMetrics(CellSize(10, 20), "not a terminal", confident=False))
        return _ENV

    probe = probe_terminal()                               # the single raw-mode excursion
    metrics = CellMetrics.from_probe(probe)
    if backend is not None:
        _ENV = GraphicsEnvironment(backend, None, metrics)
    else:
        chosen, reason = _select(probe)
        _ENV = GraphicsEnvironment(chosen, reason, metrics)
    return _ENV


def _select(probe: TerminalProbe) -> tuple[GraphicsBackend | None, GraphicsUnavailable | None]:
    """Map the probe to a backend, or to the most actionable failure reason. Pure — no I/O."""
    if not probe.supports_sixel:                           # covers both "no reply" and "replied, no sixel"
        return None, GraphicsUnavailable.NO_PROTOCOL
    if not encoder_available():                            # img2sixel soft; numpy fallback also absent
        return None, GraphicsUnavailable.ENCODER_MISSING
    return SixelBackend(), None
    # (kitty/TGP would be tried here once implemented — see render.kitty.)


# -- accessors widgets read --------------------------------------------------------------------------

def environment() -> GraphicsEnvironment:
    return _ENV


def active_backend() -> GraphicsBackend | None:
    return _ENV.backend


def unavailable_reason() -> GraphicsUnavailable | None:
    return _ENV.reason


def cell_metrics() -> CellMetrics:
    return _ENV.metrics


def note_resize(grid_cols: int, grid_rows: int, pixel_size) -> bool:
    """Live cell-size update from an app resize. Returns True if the cell size actually changed.

    ``pixel_size`` is Textual's ``Resize.pixel_size`` (a ``Size`` or None). Wire this from the app entry
    point's ``on_resize``, where the size is the whole grid::

        def on_resize(self, event: events.Resize) -> None:
            graphics.note_resize(self.size.width, self.size.height, event.pixel_size)

    No-op (returns False) when the terminal doesn't report pixel size — the seeded value then stands,
    which is correct for any pure window resize (cell px is unchanged; only the grid grew/shrank).
    """
    if pixel_size is None:
        return False
    return _ENV.metrics.update_from_pixels(grid_cols, grid_rows, pixel_size.width, pixel_size.height)
