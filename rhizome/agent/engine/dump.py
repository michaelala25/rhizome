"""Debug dumps for prompt-engine artifacts: render a wire request or a usage report as readable text, and
(unconditionally for now — see the TODO) write it to a tmp file for inspection.

Engine-agnostic by construction: every helper takes a ``ModelRequest`` / ``UsageReport`` plus an optional
node id, nothing engine-specific, so any ``PromptEngine`` can call these from its ``prepare`` to dump exactly
what it is about to send and how that prompt breaks down. The file-writers swallow failures (warn, never
raise) — a debug dump must never disturb a live model call.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import SystemMessage

from rhizome.logs import get_logger

from .metadata import lifetime_of, pin_of
from .usage import UsageReport

_logger = get_logger("agent.engine.dump")

# TODO(debug-gate): the dump_* helpers write UNCONDITIONALLY — a bring-up aid for inspecting what reaches the
# model and how the prefix budget breaks down. Gate them on a debug-mode flag once one is threaded to the
# engine (the app has a --debug flag; it just isn't plumbed this far yet).
PROMPT_DUMP_DIR = Path("/tmp/rhizome-prompt-dumps")


def content_text(content: Any) -> str:
    """Readable rendering of a message's ``content`` — a plain str, or the structured block list that
    providers use for tool calls / multimodal turns."""
    if isinstance(content, str):
        return content
    return json.dumps(content, default=str, indent=2, ensure_ascii=False)


# ----- wire request ---------------------------------------------------------- #


def format_request(request: ModelRequest, node: int | None = None) -> str:
    """Render the outgoing request as readable text: the system message first, then each message with its
    id / position / lifetime tags and full content (plus tool-call details where present)."""
    system = request.system_message
    bar = "=" * 100
    out = [
        f"prepare() dump — node={node}  messages={len(request.messages)}",
        "",
        bar, "SYSTEM MESSAGE", bar,
        system.content if isinstance(system, SystemMessage) else "(no system_message on the request)",
        "",
        bar, "MESSAGES", bar,
    ]
    for i, m in enumerate(request.messages):
        out.append(f"\n--- [{i}] {type(m).__name__}  id={m.id}  pin={pin_of(m)}  lifetime={lifetime_of(m)} ---")
        out.append(content_text(m.content))
        if getattr(m, "tool_calls", None):
            out.append(f"tool_calls: {json.dumps(m.tool_calls, default=str, indent=2)}")
        if getattr(m, "tool_call_id", None):
            out.append(f"tool_call_id: {m.tool_call_id}")
    return "\n".join(out)


def dump_request(request: ModelRequest, node: int | None = None) -> None:
    """Write ``format_request`` output to a per-call tmp file and log its path; swallow any failure."""
    try:
        PROMPT_DUMP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = PROMPT_DUMP_DIR / f"prepare-{stamp}-node{node}.txt"
        path.write_text(format_request(request, node), encoding="utf-8")
        _logger.info("Prompt dump written: %s", path)
    except Exception as exc:  # noqa: BLE001 — a debug dump must never break a model call
        _logger.warning("Prompt dump failed: %s", exc)


# ----- usage report ---------------------------------------------------------- #


def format_report(report: UsageReport, node: int | None = None) -> str:
    """Render a ``UsageReport`` as readable text: the provider ground truth and window headline, the per-kind
    rollup, then every segment (largest first) with its message id."""
    usage = report.usage
    out = [f"usage report — node={node}", ""]

    if usage is None:
        out.append("provider: (no model response on this thread yet — segments are raw estimates)")
    else:
        out.append(
            f"provider: input={usage.input_tokens} (fresh={usage.fresh_input_tokens} "
            f"cache_read={usage.cache_read_tokens} cache_creation={usage.cache_creation_tokens}) "
            f"output={usage.output_tokens} total={usage.total_tokens}"
        )
    percent = report.usage_percent
    window = f"{usage.input_tokens} / {report.max_input_tokens}" if usage and report.max_input_tokens else "?"
    out.append(f"window: {window}" + (f"  ({percent:.1f}%)" if percent is not None else ""))

    out += ["", "by kind:"]
    for kind, tokens in sorted(report.by_kind().items(), key=lambda kv: kv[1], reverse=True):
        out.append(f"  {kind:<16} {tokens:>8}")

    out += ["", "segments:"]
    for segment in sorted(report.segments, key=lambda s: s.tokens, reverse=True):
        anchor = f"  id={segment.message_id}" if segment.message_id else ""
        out.append(f"  {'[' + segment.kind + ']':<18} {segment.tokens:>8}{anchor}")
    return "\n".join(out)


def dump_report(report: UsageReport, node: int | None = None) -> None:
    """Write ``format_report`` output to a per-call tmp file and log its path; swallow any failure."""
    try:
        PROMPT_DUMP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = PROMPT_DUMP_DIR / f"usage-{stamp}-node{node}.txt"
        path.write_text(format_report(report, node), encoding="utf-8")
        _logger.info("Usage report dump written: %s", path)
    except Exception as exc:  # noqa: BLE001 — a debug dump must never break a model call
        _logger.warning("Usage report dump failed: %s", exc)
