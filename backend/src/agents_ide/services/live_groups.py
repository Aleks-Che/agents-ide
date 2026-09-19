"""Resolve external model groups at dispatch; attempt records keep their history."""

from copy import deepcopy
from typing import Any

from sqlalchemy.orm import Session

from agents_ide.errors import AppError
from agents_ide.services.settings import capture_dependencies


def candidate_identity(candidate: dict[str, Any]) -> tuple[Any, ...]:
    return (
        candidate.get("id", candidate.get("member_id")),
        candidate.get("harness_profile_id"),
        candidate.get("provider_connection_id"),
        candidate.get("model_id"),
    )


def refresh_groups(
    session: Session, snapshot: dict[str, Any], node_id: str | None = None
) -> dict[str, Any]:
    """Return a read-only projection. Missing groups must never fall back to old members."""
    dependencies: dict[str, Any] = deepcopy(snapshot.get("dependencies", {}))
    configuration = {**snapshot.get("resolved_settings", {}), "model_selections": {}}
    for node in snapshot.get("graph", {}).get("nodes", []):
        if node_id is not None and node["id"] != node_id:
            continue
        config = dependencies.get("nodes", {}).get(node["id"], {})
        if not (group_id := config.get("model_group_id")):
            continue
        selected = {
            **node,
            "config": {
                **node.get("config", {}),
                "model_selection": {"kind": "group", "group_id": group_id},
            },
        }
        try:
            fresh = capture_dependencies(session, {"nodes": [selected]}, configuration)
        except AppError as exc:
            dependencies["nodes"][node["id"]] = {
                **config,
                "candidates": [],
                "group_unavailable_reason": exc.code,
            }
            group = dependencies.get("model_groups", {}).get(group_id)
            if group:
                dependencies["model_groups"][group_id] = {**group, "members": []}
            continue
        for key in ("nodes", "harness_profiles", "provider_connections", "model_groups"):
            dependencies.setdefault(key, {}).update(fresh[key])
    return dependencies


def save_group_dependencies(runtime: dict[str, Any], dependencies: dict[str, Any]) -> None:
    runtime["group_dependencies"] = deepcopy(dependencies)
