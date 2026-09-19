"""Loop counters and per-run iteration budgets, independent of task timeouts."""

from typing import Any


def loop_key(loop: dict[str, Any], runtime: dict[str, Any]) -> str:
    return (
        f"{loop['id']}:{runtime.get('work', {}).get('scope', 'main')}"
        if loop.get("scope") == "item"
        else str(loop["id"])
    )


def loop_limit(loop: dict[str, Any], runtime: dict[str, Any]) -> int:
    return int(runtime.get("loop_limits", {}).get(loop_key(loop, runtime), loop["max_iterations"]))


def loop_progress(graph: dict[str, Any], runtime: dict[str, Any]) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    counts = runtime.get("loop_counts", {})
    for index, edge in enumerate(graph.get("edges", [])):
        if not (loop := edge.get("loop")):
            continue
        key = loop_key(loop, runtime)
        if key not in result:
            completed = int(counts.get(key, 0))
            limit = loop_limit(loop, runtime)
            item = loop.get("scope") == "item"
            result[key] = {
                "id": loop["id"],
                "key": key,
                "scope": runtime.get("work", {}).get("scope", "main") if item else "run",
                "completed": completed,
                "max_iterations": limit,
                "remaining": max(0, limit - completed),
                "total_completed": sum(
                    v for k, v in counts.items() if k.startswith(f"{loop['id']}:")
                )
                if item
                else completed,
                "edge_ids": [],
            }
        result[key]["edge_ids"].append(edge.get("id") or edge.get("edge_id") or f"edge_{index}")
    return list(result.values())
