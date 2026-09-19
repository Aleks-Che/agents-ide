"""Loop counters and per-run iteration budgets, independent of task timeouts."""

from typing import Any

from agents_ide.domain.graph_schema import edge_endpoints


def loop_body_nodes(graph: dict[str, Any], edge: dict[str, Any]) -> set[str]:
    """Nodes on paths from a loop's target back to its source, including branches.

    Stop at both boundaries so surrounding loops do not pull unrelated stages
    into this iteration. Inner loop branches remain part of the outer body.
    """
    source, target, _ = edge_endpoints(edge)
    if source is None or target is None:
        return set()
    forward: dict[str, list[str]] = {}
    reverse: dict[str, list[str]] = {}
    for link in graph.get("edges", []):
        start, end, _ = edge_endpoints(link)
        if start is not None and end is not None:
            forward.setdefault(start, []).append(end)
            reverse.setdefault(end, []).append(start)

    def reachable(start: str, stop: str, links: dict[str, list[str]]) -> set[str]:
        pending, seen = [start], set()
        while pending:
            node = pending.pop()
            if node in seen:
                continue
            seen.add(node)
            if node != stop:
                pending.extend(links.get(node, []))
        return seen

    return reachable(target, source, forward) & reachable(source, target, reverse)


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
