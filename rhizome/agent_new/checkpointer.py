"""Agent checkpointer service: SSOT for langgraph thread persistence.

One shared ``BaseCheckpointSaver`` backs every agent thread; ``{workspace_id}:{uuid}`` thread ids keep
them isolated within it. Builders take the saver and bake it into their agents, so it must outlive
template rebuilds for a thread id to keep meaning across a provider/model change.
"""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

# ==========================================================================================
# Service: AgentCheckpointerService
#   Shape : alias -- the contract is langgraph's ``BaseCheckpointSaver`` (no first-party slice)
#   Scope : workspace (one shared saver)
# ==========================================================================================
# Aliased so the dependency site reads as intent (``checkpointer: AgentCheckpointerService``).
AgentCheckpointerService = BaseCheckpointSaver


def build_checkpointer() -> AgentCheckpointerService:
    """Descriptor for the shared checkpointer. ``InMemorySaver`` for now; swap for a persistent saver
    (e.g. an async SQLite saver) once conversations need to survive process restarts."""
    return InMemorySaver()
