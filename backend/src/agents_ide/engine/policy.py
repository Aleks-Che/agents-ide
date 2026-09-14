"""Mutable budget policy; execution inputs and accumulated usage stay immutable."""

from typing import Any

DEFAULT_LIMITS = {
    "max_calls": 100,
    "max_node_visits": 1000,
    "max_backward_transitions": 200,
    "max_duration_seconds": 86400,
}


def effective_limits(snapshot: dict[str, Any], runtime: dict[str, Any]) -> dict[str, int]:
    return {
        **DEFAULT_LIMITS,
        **snapshot.get("resolved_settings", {}).get("limit_overrides", {}),
        **runtime.get("limit_overrides", {}),
    }
