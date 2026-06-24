"""Tests for EngineEventsChannel — the per-node engine→app event channel (compaction status today)."""

from rhizome.agent.engine_events import EngineEventsChannel


class Recorder:
    """Strong-referenced subscriber — CallbackHost holds callbacks weakly, so a bare lambda would die."""

    def __init__(self) -> None:
        self.started: list[tuple[int, int]] = []
        self.finished: list[int] = []

    def on_started(self, node_id: int, count: int) -> None:
        self.started.append((node_id, count))

    def on_finished(self, node_id: int) -> None:
        self.finished.append(node_id)


def test_node_id_is_exposed():
    assert EngineEventsChannel(7).node_id == 7


def test_compaction_events_carry_node_id():
    channel = EngineEventsChannel(7)
    rec = Recorder()
    channel.subscribe(channel.Callbacks.OnCompactionStarted, rec.on_started)
    channel.subscribe(channel.Callbacks.OnCompactionFinished, rec.on_finished)

    channel.compaction_started(3)
    channel.compaction_finished()

    assert rec.started == [(7, 3)]      # node id leads every payload, so a subscriber aggregating
    assert rec.finished == [7]          # several branches can tell which one fired


def test_each_node_channel_carries_its_own_id():
    a, b = EngineEventsChannel(1), EngineEventsChannel(2)
    rec = Recorder()
    a.subscribe(a.Callbacks.OnCompactionStarted, rec.on_started)
    b.subscribe(b.Callbacks.OnCompactionStarted, rec.on_started)

    a.compaction_started(5)
    b.compaction_started(9)
    assert rec.started == [(1, 5), (2, 9)]
