"""Effective harness settings shared by preflight and dispatch."""

from typing import Any


def effective_settings(
    kind: str, settings: dict[str, Any], node_config: dict[str, Any]
) -> dict[str, Any]:
    return {**settings, **node_config.get("harness_settings", {}).get(kind, {})}


def approval_policy(settings: dict[str, Any]) -> str:
    if "auto_approve" in settings:
        return "never" if settings["auto_approve"] else "on-request"
    return str(settings.get("approval_policy", "never"))
