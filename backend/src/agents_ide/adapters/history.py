"""User history excludes vendor telemetry and tool input/output bodies."""

from collections.abc import Iterable
from typing import Any

OMITTED_HISTORY_EVENTS = frozenset(
    {"agent.native_event", "agent.output_delta", "agent.tool_call", "agent.plan_updated"}
)


def tool_summaries(calls: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only bounded hints for interrupted-task handoff, never command output."""
    result = []
    for call in calls:
        item = call.get("item", call)
        if not isinstance(item, dict):
            continue
        result.append(
            {
                "tool": str(item.get("tool") or item.get("type") or "")[:128],
                "status": str(item.get("status") or call.get("phase") or "")[:64],
                "summary": str(item.get("summary") or item.get("command") or "")[:500],
            }
        )
    return result[-20:]
