"""Per-node engine→app event channel.

``EngineEventsChannel`` is the side-band the prompt engine pushes status through — the things the normal
token/tool stream doesn't carry. Today that is slow context compaction (summarizing reclaimed messages):
the engine signals start/finish so the app can show a spinner without the wait looking like a hang.

One channel per conversation node (created in ``ConversationGraph._make_node``, wired into that node's
compile context), constructed with its node id. Every callback carries that node id as its FIRST argument,
so a subscriber aggregating several branches can tell which one fired — and a view watching one branch
won't spinner because a *different* branch is compacting. A pure ``CallbackHost``: it holds no state, only
the subscriber lists; the engine emits, the app subscribes.

The ``compaction_*`` emitters are the channel's contract; their call sites land with the ``summarize``
cleanup strategy (the only reclamation slow enough to be worth a spinner — stubbing is instant).
"""

from rhizome.utils.callbacks import CallbackHost


class EngineEventsChannel(CallbackHost):
    """Engine→app status events for one conversation node. See the module docstring."""

    class Callbacks:
        OnCompactionStarted  = "OnCompactionStarted"   # (node_id: int, count: int)
        OnCompactionFinished = "OnCompactionFinished"  # (node_id: int)

    def __init__(self, node_id: int) -> None:
        super().__init__()
        self._node_id = node_id
        self.make_callback_groups({
            self.Callbacks.OnCompactionStarted:  (int, int),
            self.Callbacks.OnCompactionFinished: (int,),
        })

    @property
    def node_id(self) -> int:
        return self._node_id

    def compaction_started(self, count: int) -> None:
        """Fire ``OnCompactionStarted(node_id, count)`` — slow reclamation of ``count`` message(s) began."""
        self.emit(self.Callbacks.OnCompactionStarted, self._node_id, count)

    def compaction_finished(self) -> None:
        """Fire ``OnCompactionFinished(node_id)`` — the in-flight compaction settled (success or failure)."""
        self.emit(self.Callbacks.OnCompactionFinished, self._node_id)
