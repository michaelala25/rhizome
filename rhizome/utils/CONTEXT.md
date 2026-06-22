# rhizome/utils/

> **FOR AGENTS** — A hand-maintained orientation doc for `rhizome/utils/`. Edit it when the maintainer
> asks, and keep its style: high-level and human-readable, describing what each piece is and how the
> service convention works rather than cataloguing methods — the code (and each service's own header)
> is the source of truth for detail.

Cross-cutting infrastructure with no domain knowledge of its own — the primitives the rest of the app
is built on. Nothing here imports from `rhizome.app` / `rhizome.agent` / `rhizome.db`; the
dependency only ever points inward.


## What lives here

- **`services.py`** — the dependency-injection container (`ServiceAccessor`), plus `Handle`, `ServiceMeta`,
  and the errors. A factory declares its dependencies as annotated parameters and the container resolves
  them; see the module docstring for the full model (scopes, strong/weak edges, provenance).
- **`workers.py`** — worker scheduling: the `WorkerScheduler` callable, the `WorkerSchedulerService`
  holder, and its `WorkerSchedulerBinding` impl (lets a view-model schedule widget-bound work without
  importing Textual).
- **`callbacks.py`** — `CallbackHost` / `CallbackGroup`: the weak-subscriber event primitive under the
  app's view-model channels.
- **`data_structures/`** — general containers: a structure-only directed `Graph`, and the `MergeTree`
  (rooted DAG addressed by root-to-node `Path`s) layered on it.


## Defining a service

The DI *container* lives here, but the services themselves live with the code that implements them —
`APIKeyService` in `credentials.py`, `SessionFactoryService` in `db/engine.py`, the agent services in
`agent/`, and so on. To find them all regardless of where they live, grep the header token:

```
grep -rn "^# Service:" rhizome/
```

Every service carries that header — a uniform micro-schema giving its shape and registration scope at a
glance — and follows one naming rule:

> **`XxxService` always names the injected contract (the Protocol / DI key). The implementation gets a
> distinct, descriptive name and explicitly subclasses `XxxService`.**

The explicit subclass is the readable `implements` marker; the type-checker enforces that the impl
actually covers the contract. (`issubclass`/`isinstance` against the Protocol still raise at runtime
unless it is `@runtime_checkable` — the container never relies on them, it keys on the type itself.)

```python
# ==========================================================================================
# Service: APIKeyService
#   Shape : protocol + first-party impl (CredentialsAPIKeyService, below)
#   Scope : root
# ==========================================================================================

class APIKeyService(Protocol):
    def get(self, provider: str) -> str | None: ...

class CredentialsAPIKeyService(APIKeyService):   # explicit subclass — reads as `implements`
    ...
```

### Shapes and their exceptions

One rule, a few shapes — and each service states its own in its header, so the exceptions are
self-documenting (you never have to come back here to learn why one looks different):

- **`protocol + impl`** — the default above; the first-party impl subclasses the Protocol.
- **`protocol (structural)`** — the impl can't subclass, so it satisfies the contract by shape. Two
  reasons: an impl you don't own (`SessionFactoryService` is also satisfied by SQLAlchemy's
  `async_sessionmaker`), or a metaclass conflict (`Options`' `OptionsMeta` can't co-exist with
  `Protocol`'s metaclass). The header's `Notes` line says which.
- **`alias`** — there is no first-party contract worth defining, so `XxxService` is an alias for the
  real type: the whole concrete class when consumers need all of it (`AgentRuntimeService = AgentRuntime`),
  or a third-party base when the library owns the contract (`AgentCheckpointerService = BaseCheckpointSaver`).

### Keep services extractable

The definitions are co-located with their impls today. To keep the option open of lifting them into a
shared services layer later (one at a time, no big disentangle), hold to one principle when writing a new
one: **a service interface — and the value-types its signatures mention — should depend only "downward"**
— on stdlib/typing, third-party leaf types, or small DTOs that import nothing from the subsystem. If an
interface reaches for a type that lives in its impl's module (the way `OptionService` needs `OptionSpec`),
that type wants to move to a contract layer first. New services come out extractable by construction; the
entangled existing ones can be lifted opportunistically.
