"""Stylesheet instrumentation layered alongside the in-app pyinstrument profiler.

pyinstrument is a sampling profiler — it captures stack frames but never function arguments, so a
flame graph entry like ``Stylesheet._process_component_classes`` tells you *that* the call happened
but not *which widget* triggered it. To answer that, we monkey-patch four ``Stylesheet`` methods
that dominate the focus-change flame graph and key the cost off ``type(node).__name__`` +
``node.id``.

Patched methods (rough call frequency, slowest at top):

  * ``Stylesheet.apply``                       — per-widget rule application; the outer entry point
  * ``Stylesheet._process_component_classes``  — runs ``apply`` on a virtual node per component class
  * ``Stylesheet.replace_rules``               — final rules-map swap on the node (mostly diffing)
  * ``Stylesheet._check_rule``                 — selector match per ``RuleSet``; the hottest path,
                                                  so wrapper overhead is most visible here

Patching is paired ``start_stylesheet_instrumentation`` / ``stop_stylesheet_instrumentation`` — the
stop call restores the originals and returns a multi-section text report.

Cost-model note: ``_process_component_classes`` calls ``self.apply(virtual_node)`` internally, so
``apply``'s totals include re-entrant time from PCC. To isolate "outer" apply time, subtract
``_process_component_classes`` total time from ``apply`` total time. The report flags this.
"""

from __future__ import annotations

import time
from typing import Any, Callable


# ----------------------------------------------------------------------------------------------------------
# State — module-level singletons, paired with the start/stop calls in the app's profile toggle.
# ----------------------------------------------------------------------------------------------------------

_NodeKey = tuple[str, str | None]

# Per-method bucket: ``method_name -> { (widget_class_name, widget_id_or_None): [count, total_ns] }``.
# Value is a mutable list (not a tuple) so the hot ``_record`` path can update in place.
_records: dict[str, dict[_NodeKey, list[int]]] = {}

# Originals stashed at patch time. For instance methods this is the function; for classmethods it's
# the classmethod descriptor itself (so ``setattr`` restores it cleanly through the descriptor protocol).
_originals: dict[str, Any] = {}

_active: bool = False


# ----------------------------------------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------------------------------------

def start_stylesheet_instrumentation() -> None:
    """Patch the four ``Stylesheet`` methods. Idempotent — calls while already-active are no-ops."""
    global _active
    if _active:
        return
    _records.clear()
    _originals.clear()

    # Imported lazily so the helper doesn't drag Textual in for callers that never profile.
    from textual.css.stylesheet import Stylesheet

    _patch_instance_method(Stylesheet, "apply", _wrap_apply)
    _patch_instance_method(Stylesheet, "_process_component_classes", _wrap_process_component_classes)
    _patch_classmethod(Stylesheet, "_check_rule", _wrap_check_rule)
    _patch_classmethod(Stylesheet, "replace_rules", _wrap_replace_rules)

    _active = True


def stop_stylesheet_instrumentation() -> str:
    """Restore the originals and return a multi-section text report. Returns ``""`` if inactive."""
    global _active
    if not _active:
        return ""

    from textual.css.stylesheet import Stylesheet
    for name, original in _originals.items():
        setattr(Stylesheet, name, original)

    report = _build_report()
    _records.clear()
    _originals.clear()
    _active = False
    return report


# ----------------------------------------------------------------------------------------------------------
# Patch helpers — instance methods vs classmethods need different machinery
# ----------------------------------------------------------------------------------------------------------

def _patch_instance_method(cls: type, name: str, wrapper_factory: Callable[[Any, str], Any]) -> None:
    """Stash the original function and install the factory's wrapper. ``setattr`` on a class slot
    is enough — Python's method binding handles ``self`` at call time."""
    original = getattr(cls, name)
    _originals[name] = original
    setattr(cls, name, wrapper_factory(original, name))


def _patch_classmethod(cls: type, name: str, wrapper_factory: Callable[[Any, str], Any]) -> None:
    """Stash the original classmethod *descriptor* (so we can ``setattr`` it back unchanged), unwrap
    the underlying function to feed the factory, then re-wrap the result as ``classmethod``."""
    original_descriptor = cls.__dict__[name]
    _originals[name] = original_descriptor
    original_func = original_descriptor.__func__
    setattr(cls, name, classmethod(wrapper_factory(original_func, name)))


# ----------------------------------------------------------------------------------------------------------
# Wrappers — one per method shape, each closing over the original + method name
# ----------------------------------------------------------------------------------------------------------

def _node_key(node: Any) -> _NodeKey:
    """``(class_name, id_or_None)`` — best-effort, since virtual nodes may not have an ``id``."""
    try:
        return (type(node).__name__, getattr(node, "id", None))
    except Exception:
        return ("?", None)


def _record(method: str, key: _NodeKey, dt_ns: int) -> None:
    bucket = _records.get(method)
    if bucket is None:
        bucket = {}
        _records[method] = bucket
    entry = bucket.get(key)
    if entry is None:
        bucket[key] = [1, dt_ns]
    else:
        entry[0] += 1
        entry[1] += dt_ns


def _wrap_apply(original: Callable[..., Any], name: str) -> Callable[..., Any]:
    def wrapped(self, node, *, animate=False, cache=None):
        t0 = time.perf_counter_ns()
        try:
            return original(self, node, animate=animate, cache=cache)
        finally:
            _record(name, _node_key(node), time.perf_counter_ns() - t0)
    return wrapped


def _wrap_process_component_classes(original: Callable[..., Any], name: str) -> Callable[..., Any]:
    def wrapped(self, node):
        t0 = time.perf_counter_ns()
        try:
            return original(self, node)
        finally:
            _record(name, _node_key(node), time.perf_counter_ns() - t0)
    return wrapped


def _wrap_check_rule(original: Callable[..., Any], name: str) -> Callable[..., Any]:
    # ``_check_rule`` gets a root → leaf node chain; the leaf is the widget being styled. Keying
    # cost off the leaf rather than the rule itself makes the report widget-centric like the others.
    def wrapped(cls, rule_set, css_path_nodes):
        t0 = time.perf_counter_ns()
        try:
            return original(cls, rule_set, css_path_nodes)
        finally:
            leaf = css_path_nodes[-1] if css_path_nodes else None
            _record(name, _node_key(leaf), time.perf_counter_ns() - t0)
    return wrapped


def _wrap_replace_rules(original: Callable[..., Any], name: str) -> Callable[..., Any]:
    def wrapped(cls, node, rules, animate=False):
        t0 = time.perf_counter_ns()
        try:
            return original(cls, node, rules, animate)
        finally:
            _record(name, _node_key(node), time.perf_counter_ns() - t0)
    return wrapped


# ----------------------------------------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------------------------------------

_TOP_N = 20
_RULE = "-" * 110


def _build_report() -> str:
    if not _records:
        return "(no Stylesheet calls recorded)\n"

    lines: list[str] = []
    lines.append("=" * 110)
    lines.append("Stylesheet instrumentation report")
    lines.append("=" * 110)
    lines.append("")
    lines.append(
        "Note: ``apply`` totals include re-entrant calls from ``_process_component_classes`` "
        "(which calls"
    )
    lines.append(
        "      ``apply`` on a virtual node per component class). To isolate outer-apply time, "
        "subtract"
    )
    lines.append("      the ``_process_component_classes`` total from the ``apply`` total.")
    lines.append("")

    lines.append("Per-method totals:")
    for method, bucket in _records.items():
        total_calls = sum(c for c, _ in bucket.values())
        total_ns = sum(n for _, n in bucket.values())
        lines.append(
            f"  Stylesheet.{method:<34s}  {total_calls:>12,} calls   {total_ns / 1e9:>9.3f}s"
        )
    lines.append("")

    for method, bucket in _records.items():
        total_ns = sum(n for _, n in bucket.values())
        lines.append(_RULE)
        lines.append(f"Stylesheet.{method}")
        lines.append(_RULE)
        lines.append("")

        # By class only — collapses widgets of the same type regardless of id.
        by_class: dict[str, list[int]] = {}
        for (cls_name, _id), (count, ns) in bucket.items():
            agg = by_class.get(cls_name)
            if agg is None:
                by_class[cls_name] = [count, ns]
            else:
                agg[0] += count
                agg[1] += ns

        lines.append(f"  Top {_TOP_N} by total time (class):")
        for cls_name, (count, ns) in sorted(by_class.items(), key=lambda x: -x[1][1])[:_TOP_N]:
            pct = ns / total_ns * 100 if total_ns else 0
            lines.append(
                f"    {cls_name:<48s}  {count:>12,} calls   {ns / 1e9:>9.3f}s  ({pct:5.1f}%)"
            )
        lines.append("")

        # By class + id — shows whether one specific widget instance is the outlier.
        lines.append(f"  Top {_TOP_N} by total time (class + id):")
        for (cls_name, id_), (count, ns) in sorted(bucket.items(), key=lambda x: -x[1][1])[:_TOP_N]:
            tag = f"{cls_name}#{id_}" if id_ else cls_name
            pct = ns / total_ns * 100 if total_ns else 0
            lines.append(
                f"    {tag:<68s}  {count:>12,} calls   {ns / 1e9:>9.3f}s  ({pct:5.1f}%)"
            )
        lines.append("")

    return "\n".join(lines) + "\n"
