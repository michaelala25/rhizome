# ResourceLoader → ResourceManager State Cases

This document enumerates the different user interactions in the ResourceLoader
and the resulting state sent to the `ResourceManager` via `_sync_manager_state`.

## Representation

State is a **minimum-description-length (MDL)** dict:

```python
dict[NodeKey, LoadMode]   # NodeKey = tuple[Literal["resource","section"], int]
```

An entry at node *X* means "*X* and every descendant of *X* are loaded at this
mode, unless a descendant has its own entry overriding it."  A node that is
UNLOADED has no entry at all.  The loader maintains two invariants:

1. No ancestor–descendant pair exists at the same mode (always collapsed).
2. If every direct child of a node *P* has an entry at the same mode *M*,
   the children are collapsed into a single entry `P: M`.

## Assumptions

All examples use a resource **R** (id=1) with sections **S1** (id=10),
**S2** (id=20), **S3** (id=30).  Where nesting matters, S2 has a child
**S2a** (id=25).

`loading_preference=auto` and `estimated_tokens > 10_000` (so *space*
resolves to `LoadMode.LOADED`).  Embeddings are assumed to already exist
unless explicitly noted.

---

## Cases

### 1. Load full resource (no sections)

R has no sections. User presses `space` on R.

```
{("resource", 1): LOADED}
```

### 2. Load full resource (with sections)

User presses `space` on R.

```
{("resource", 1): LOADED}
```

(Section entries are implicit.  `S1`, `S2`, `S2a`, `S3` all inherit `LOADED`.)

### 3. Context-stuff full resource (no sections)

User presses `ctrl+j` on R.

```
{("resource", 1): CONTEXT_STUFFED}
```

### 4. Context-stuff full resource (with sections)

User presses `ctrl+j` on R.

```
{("resource", 1): CONTEXT_STUFFED}
```

### 5. Load a single section of an unloaded resource

Everything unloaded.  User presses `space` on S2.

```
{("section", 20): LOADED}
```

(S2a inherits `LOADED` implicitly.  S1, S3, R are absent from the state —
`_effective_mode` returns `None` for them.)

### 6. Context-stuff a single section of an unloaded resource

Everything unloaded.  User presses `ctrl+j` on S2.

```
{("section", 20): CONTEXT_STUFFED}
```

### 7. Loaded full resource, user unloads a section and its children

Starting state: `{("resource", 1): LOADED}`.  User presses `space` on S2.

The toggle expands R into its children, then removes S2:

```
{("section", 10): LOADED, ("section", 30): LOADED}
```

(S2 is absent; S1 and S3 remain at `LOADED`.  S2a inherits S2's absence, so
it's unloaded too.)

### 8. Loaded full resource, user unloads multiple non-sibling sections

Starting: `{("resource", 1): LOADED}`.  User presses `space` on S1, then S3.

After first unload: `{("section", 20): LOADED, ("section", 30): LOADED}`.
After second unload: `{("section", 20): LOADED}`.

### 9. Loaded full resource, user context-stuffs a section

Starting: `{("resource", 1): LOADED}`.  User presses `ctrl+j` on S2.

Expand R into children, toggle S2 to CS, check for collapse (modes diverge
so no collapse):

```
{
  ("section", 10): LOADED,
  ("section", 20): CONTEXT_STUFFED,
  ("section", 30): LOADED,
}
```

(S2a inherits S2's `CONTEXT_STUFFED`.)

### 10. Context-stuffed full resource, user demotes a section to default load

Starting: `{("resource", 1): CONTEXT_STUFFED}`.  User presses `space` on S2.

Expand R's CS entry to children, toggle S2 to LOADED:

```
{
  ("section", 10): CONTEXT_STUFFED,
  ("section", 20): LOADED,
  ("section", 30): CONTEXT_STUFFED,
}
```

### 11. Partially loaded resource, user loads the resource root to fill the rest

Starting: `{("section", 10): LOADED}` (only S1 loaded).  User presses `space`
on R.

The toggle clears all descendant entries under R and sets R to `LOADED`:

```
{("resource", 1): LOADED}
```

### 12. Partially context-stuffed, user context-stuffs the root

Starting: `{("section", 10): CONTEXT_STUFFED}`.  User presses `ctrl+j` on R.

```
{("resource", 1): CONTEXT_STUFFED}
```

### 13. User unloads every section individually (auto-collapse to empty)

Starting: `{("resource", 1): LOADED}`.  User presses `space` on S1, then S3,
then S2.

After unloading S1: `{("section", 20): LOADED, ("section", 30): LOADED}`.
After unloading S3: `{("section", 20): LOADED}`.
After unloading S2: `{}`.

(Collapse-on-unload is a no-op — entries just disappear.)

### 14. User toggles all sections individually to the same mode (collapse on agreement)

Starting: `{}`.  User presses `ctrl+j` on S1, S2, S3 one at a time.

- After S1: `{("section", 10): CS}`.
- After S2: `{("section", 10): CS, ("section", 20): CS}`.
- After S3: collapse triggers because all children of R now agree.

```
{("resource", 1): CONTEXT_STUFFED}
```

### 15. Embedding pending (no pre-existing embeddings)

Same as case 2, but `_needs_embeddings(R)` returns `True`.

- The toggle writes `{("resource", 1): LOADED}` to the loader's state.
- R is added to `_pending_resources`.  `_sync_manager_state` **filters out**
  pending-resource entries, so the manager sees `{}` for now.
- The tree shows a spinner on R and greys out its sections.

On **success**: R is removed from `_pending_resources`, `_sync_manager_state`
runs again, manager sees `{("resource", 1): LOADED}`.

On **failure**: R is removed from `_pending_resources` **and** the loader
pops `("resource", 1)` plus every descendant entry.  Manager sees `{}`.

---

## Summary table

| # | Action | MDL state |
|---|--------|-----------|
| 1 | Load resource (no sections) | `{("resource", 1): LOADED}` |
| 2 | Load resource (with sections) | `{("resource", 1): LOADED}` |
| 3 | Context-stuff resource (no sections) | `{("resource", 1): CS}` |
| 4 | Context-stuff resource (with sections) | `{("resource", 1): CS}` |
| 5 | Load one section | `{("section", 20): LOADED}` |
| 6 | Context-stuff one section | `{("section", 20): CS}` |
| 7 | Full load, unload one section tree | `{("section", 10): LOADED, ("section", 30): LOADED}` |
| 8 | Full load, unload multiple sections | `{("section", 20): LOADED}` |
| 9 | Full load, context-stuff one section | `{S1: LOADED, S2: CS, S3: LOADED}` |
| 10 | Full CS, demote one section to load | `{S1: CS, S2: LOADED, S3: CS}` |
| 11 | Partial load, load root to fill | `{("resource", 1): LOADED}` |
| 12 | Partial CS, CS root to fill | `{("resource", 1): CS}` |
| 13 | Unload all sections individually | `{}` |
| 14 | Toggle all sections to same mode | `{("resource", 1): <mode>}` (promoted) |
| 15 | Embedding pending | (deferred) |
