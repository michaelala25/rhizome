"""Dump every keybinding declared under ``rhizome/tui`` (excluding ``widgets/legacy``) to markdown.

Doubles as a proof-of-concept for import-time binding discovery: it imports every module under
``rhizome.tui`` so each ``DOMNode`` subclass' ``__init_subclass__`` has fired, then walks
``DOMNode.__subclasses__()`` and reads each class' *own* ``BINDINGS`` (not the MRO-merged set, so we
don't drag in idless framework bindings inherited from DataTable / Tree / TextArea / etc.).

Run:  uv run python scripts/dump_keybindings.py
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "keybindings_inventory.md"
sys.path.insert(0, str(REPO_ROOT))

from textual.binding import Binding
from textual.dom import DOMNode

import rhizome.tui as tui_pkg
from rhizome.tui.keybindings import is_private


def import_all_tui_modules() -> list[str]:
    """Import every module under ``rhizome.tui`` except ``widgets/legacy``. Returns import failures."""
    failures: list[str] = []
    for _finder, name, _ispkg in pkgutil.walk_packages(tui_pkg.__path__, tui_pkg.__name__ + "."):
        if ".widgets.legacy" in name or name.endswith("__main__"):
            continue
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 — report, don't abort the whole dump
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    return failures


def all_subclasses(root: type) -> set[type]:
    seen: set[type] = set()
    stack = [root]
    while stack:
        cls = stack.pop()
        for sub in cls.__subclasses__():
            if sub not in seen:
                seen.add(sub)
                stack.append(sub)
    return seen


def binding_rows(raw_bindings) -> list[dict]:
    """Normalize a class' raw ``BINDINGS`` entries (tuple- or Binding-form) into flat dicts."""
    rows: list[dict] = []
    for entry in raw_bindings:
        if isinstance(entry, tuple):
            key, action = entry[0], entry[1]
            desc = entry[2] if len(entry) > 2 else ""
            rows.append({"form": "tuple", "key": key, "action": action, "desc": desc,
                         "show": True, "id": None, "priority": False, "extra": ""})
        elif isinstance(entry, Binding):
            extra_parts = []
            if entry.key_display:
                extra_parts.append(f"key_display={entry.key_display!r}")
            if entry.tooltip:
                extra_parts.append("tooltip")
            if entry.system:
                extra_parts.append("system")
            if entry.group:
                extra_parts.append("group")
            rows.append({"form": "Binding", "key": entry.key, "action": entry.action,
                         "desc": entry.description, "show": entry.show, "id": entry.id,
                         "priority": entry.priority, "extra": ", ".join(extra_parts)})
        else:
            rows.append({"form": type(entry).__name__, "key": "?", "action": str(entry),
                         "desc": "", "show": "?", "id": None, "priority": "?", "extra": ""})
    return rows


def md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ") if text else ""


def main() -> None:
    failures = import_all_tui_modules()

    classes = [
        cls for cls in all_subclasses(DOMNode)
        if cls.__module__.startswith("rhizome.tui")
        and ".widgets.legacy" not in cls.__module__
        and "BINDINGS" in cls.__dict__
        and cls.__dict__["BINDINGS"]
    ]

    # Group by source file, ordered by path; classes within a file ordered by definition line.
    by_file: dict[Path, list[type]] = {}
    for cls in classes:
        src = Path(inspect.getsourcefile(cls))
        by_file.setdefault(src, []).append(cls)
    for file_classes in by_file.values():
        file_classes.sort(key=lambda c: inspect.getsourcelines(c)[1])

    # A class counts as "migrated" once every one of its bindings carries an id (whether via a shared
    # ``Keybind`` concept or an inline ``Binding``).
    def is_migrated(cls) -> bool:
        rows = binding_rows(cls.__dict__["BINDINGS"])
        return bool(rows) and all(r["id"] for r in rows)

    total_classes = len(classes)
    total_bindings = sum(len(cls.__dict__["BINDINGS"]) for cls in classes)
    with_id = sum(1 for cls in classes for r in binding_rows(cls.__dict__["BINDINGS"]) if r["id"])
    tuple_form = sum(1 for cls in classes for r in binding_rows(cls.__dict__["BINDINGS"])
                     if r["form"] == "tuple")
    migrated = [cls for cls in classes if is_migrated(cls)]
    unmigrated = [cls for cls in classes if not is_migrated(cls)]

    # Ids carried by >1 class — the shared ``Keybind`` concepts. Purely informational: a read on how global
    # each binding is (which the namespace already signals). We don't flag clashes here — hosts wiring one
    # concept to different action names is by design (explicit action per host); clash handling is Textual's
    # job (see keybindings.py).
    id_to_classes: dict[str, list[str]] = {}
    for cls in classes:
        for r in binding_rows(cls.__dict__["BINDINGS"]):
            if not r["id"]:
                continue
            owners = id_to_classes.setdefault(r["id"], [])
            if cls.__name__ not in owners:
                owners.append(cls.__name__)
    shared_ids = {bid: owners for bid, owners in id_to_classes.items() if len(owners) > 1}

    lines: list[str] = []
    lines.append("# Keybinding Inventory — `rhizome/tui` (excluding `widgets/legacy`)")
    lines.append("")
    lines.append(f"_Generated by `scripts/dump_keybindings.py` via import-time introspection of "
                 f"`DOMNode.__subclasses__()`._")
    lines.append("")
    lines.append(f"- **{total_classes}** binding-declaring classes — "
                 f"**{len(migrated)} migrated** (every binding has an `id`), "
                 f"**{len(unmigrated)} to go**")
    lines.append(f"- **{total_bindings}** raw binding entries (as declared; comma-keys not expanded)")
    lines.append(f"- **{with_id}** already carry an `id`; **{tuple_form}** are tuple-form "
                 f"(can't carry an `id` until converted to `Binding(...)`)")
    lines.append("")
    if unmigrated:
        lines.append("**Not yet migrated (to-do):** "
                     + ", ".join(f"`{c.__name__}`" for c in sorted(unmigrated, key=lambda c: c.__name__)))
        lines.append("")
    if shared_ids:
        lines.append("**Shared ids** (one concept used by several widgets — a read on how global each "
                     "binding is; hosts may wire it to their own action names):")
        for bid in sorted(shared_ids):
            owners = ", ".join(f"`{c}`" for c in shared_ids[bid])
            lines.append(f"- `{bid}` → {owners}")
        lines.append("")
    lines.append("**Legend / gotchas:**")
    lines.append("- ✅ = every binding has an `id`; ⬜ = not yet. 🔒 = private id (a dotted segment starts "
                 "with `_`) — excluded from the future keybindings.json. The `other` column shows `system` "
                 "(hidden from the HelpPanel sidebar).")
    lines.append("- `show` is the *declared* value. Textual forces `show = bool(description and show)`, "
                 "so a binding with no description never shows regardless. **No `Footer` is mounted "
                 "anywhere yet**, so `show` currently has no visible effect — widgets render their own "
                 "hint rows.")
    lines.append("- Tuple-form bindings (`(\"key\", \"action\", \"desc\")`) can't carry an `id`.")
    lines.append("- Bindings inherited from Textual builtins (DataTable / Tree / Input / TextArea cursor "
                 "movement, etc.) are **not** listed — they have no `id` in this Textual version and "
                 "live on the framework classes, not ours.")
    if failures:
        lines.append("")
        lines.append("> ⚠️ Modules that failed to import (their bindings may be missing):")
        for f in failures:
            lines.append(f">   - {f}")
    lines.append("")

    for src in sorted(by_file, key=lambda p: str(p)):
        rel = src.relative_to(REPO_ROOT)
        lines.append(f"## `{rel}`")
        lines.append("")
        for cls in by_file[src]:
            bases = ", ".join(b.__name__ for b in cls.__bases__)
            rows = binding_rows(cls.__dict__["BINDINGS"])
            forms = {r["form"] for r in rows}
            form_note = "/".join(sorted(forms))
            status = "✅ ids" if is_migrated(cls) else "⬜ no ids yet"
            lines.append(f"### `{cls.__name__}`  ·  {status}  ·  _bases: {bases}_  ·  _{form_note}-form_")
            lines.append("")
            lines.append("| Key | Action | Description | show | prio | id | other |")
            lines.append("|-----|--------|-------------|:----:|:----:|----|-------|")
            for r in rows:
                show = r["show"]
                show_cell = "✓" if show is True else ("✗" if show is False else str(show))
                prio_cell = "✓" if r["priority"] is True else ""
                id_cell = (f"`{r['id']}`" + (" 🔒" if is_private(r["id"]) else "")) if r["id"] else ""
                lines.append(
                    f"| `{md_escape(r['key'])}` | `{md_escape(r['action'])}` | "
                    f"{md_escape(r['desc'])} | {show_cell} | {prio_cell} | {id_cell} | "
                    f"{md_escape(r['extra'])} |"
                )
            lines.append("")

    OUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {OUT_PATH} — {total_classes} classes, {total_bindings} bindings "
          f"({with_id} with id, {tuple_form} tuple-form). Import failures: {len(failures)}")


if __name__ == "__main__":
    main()
