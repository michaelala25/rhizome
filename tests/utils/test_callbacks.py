"""CallbackHost mechanics: registration, dispatch, weakref lifetime, emit_once batching."""

import gc

import pytest

from rhizome.utils.callbacks import CallbackHost, Emitter


class Host(CallbackHost):
    class Callbacks:
        OnThing = "OnThing"
        OnOther = "OnOther"

    def __init__(self):
        super().__init__()
        self.make_callback_groups({
            self.Callbacks.OnThing: int,
            self.Callbacks.OnOther: None,
        })


class Recorder:
    """Subscriber holder — bound methods keep the weakref machinery happy."""

    def __init__(self):
        self.calls = []

    def on_thing(self, value: int) -> None:
        self.calls.append(value)

    def boom(self, value: int) -> None:
        raise RuntimeError("bad subscriber")


def test_emit_by_key_and_unsubscribe():
    host, rec = Host(), Recorder()
    host.subscribe(Host.Callbacks.OnThing, rec.on_thing)
    host.emit(Host.Callbacks.OnThing, 1)
    host.emit(Host.Callbacks.OnThing, 2)
    assert rec.calls == [1, 2]

    host.unsubscribe(Host.Callbacks.OnThing, rec.on_thing)
    host.emit(Host.Callbacks.OnThing, 3)
    assert rec.calls == [1, 2]


def test_unknown_key_raises_with_available_keys():
    host = Host()
    with pytest.raises(KeyError, match="OnThing"):
        host.emit("Nope")


def test_duplicate_registration_rejected():
    host = Host()
    with pytest.raises(ValueError, match="already registered"):
        host.make_callback_group(Host.Callbacks.OnThing)


def test_dead_subscribers_are_pruned_and_exceptions_isolated():
    host, alive, dead = Host(), Recorder(), Recorder()
    bad = Recorder()
    host.subscribe(Host.Callbacks.OnThing, dead.on_thing)
    host.subscribe(Host.Callbacks.OnThing, bad.boom)
    host.subscribe(Host.Callbacks.OnThing, alive.on_thing)

    del dead
    gc.collect()

    # The GC'd subscriber is skipped, the raising one is isolated, the healthy one still fires.
    host.emit(Host.Callbacks.OnThing, 7)
    assert alive.calls == [7]


def test_emit_once_coalesces_and_strict_conflicts_raise():
    host, rec = Host(), Recorder()
    host.subscribe(Host.Callbacks.OnThing, rec.on_thing)

    with host.emit_once(Host.Callbacks.OnThing) as emitter:
        emitter.emit(Host.Callbacks.OnThing, 5)
        emitter.emit(Host.Callbacks.OnThing, 5)  # identical repeat coalesces
        assert rec.calls == []                   # deferred until exit
    assert rec.calls == [5]

    with pytest.raises(ValueError, match="conflicting args"):
        with host.emit_once(Host.Callbacks.OnThing) as emitter:
            emitter.emit(Host.Callbacks.OnThing, 1)
            emitter.emit(Host.Callbacks.OnThing, 2)


def test_emit_once_merge_last_and_non_batched_fall_through():
    host, rec = Host(), Recorder()
    host.subscribe(Host.Callbacks.OnThing, rec.on_thing)

    with host.emit_once(
        Host.Callbacks.OnOther, merge_strategy=Emitter.MergeStrategy.LAST
    ) as emitter:
        emitter.emit(Host.Callbacks.OnThing, 1)  # not in the batched set: immediate
        assert rec.calls == [1]


def test_emitters_are_single_host():
    a, b = Host(), Host()
    group_b = b._callbacks[Host.Callbacks.OnThing]
    with pytest.raises(ValueError, match="single-host"):
        with a.emit_once() as emitter:
            emitter.emit(group_b, 1)
