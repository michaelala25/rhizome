import time


class Timer:
    """A simple start/pause/stop stopwatch.

    All three mutating calls are lenient — calling them in a state where they don't apply is a no-op rather
    than an error, so callers don't have to track state themselves. The one exception is ``start()`` after
    ``stop()``: ``stop`` is terminal (you've committed to a final elapsed value), so restarting requires an
    explicit ``reset()``.
    """

    def __init__(self):
        self._started = False
        self._paused = False
        self._stopped = False
        self._start_time: float | None = None
        self._accumulated: float = 0.0  # elapsed time from previous run segments

    @property
    def started(self) -> bool:
        return self._started

    @property
    def running(self) -> bool:
        """True iff the timer is actively ticking — started, not paused, not stopped."""
        return self._started and not self._paused and not self._stopped

    def start(self):
        """Start the timer or resume it from paused. Idempotent if running. Raises if the timer was
        previously ``stop()``-ed (stop is terminal)."""
        if self._stopped:
            raise RuntimeError("Cannot start a stopped timer. Call reset() first.")
        if self.running:
            return
        self._started = True
        self._paused = False
        self._start_time = time.perf_counter()

    def pause(self):
        """Pause the timer. No-op if not running."""
        if not self.running:
            return
        self._accumulated += time.perf_counter() - self._start_time
        self._start_time = None
        self._paused = True

    def stop(self) -> float:
        """Finalize the timer and return the total elapsed time. No-op (returns the accumulated total) if
        already stopped."""
        if self._stopped:
            return self._accumulated
        if self.running:
            self._accumulated += time.perf_counter() - self._start_time
        self._start_time = None
        self._stopped = True
        return self._accumulated

    def reset(self):
        self._started = False
        self._paused = False
        self._stopped = False
        self._start_time = None
        self._accumulated = 0.0

    def elapsed(self) -> float:
        if not self._started:
            return 0.0

        if self._paused or self._stopped:
            return self._accumulated

        return self._accumulated + (time.perf_counter() - self._start_time)
