"""Build an isolated run from one version node without modifying its source."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Protocol

from agents_ide.domain.graph_ast import template_references
from agents_ide.domain.schemas import (
    DirectAgentSelection,
    DirectLLMSelection,
    SingleAgentSpec,
)
from agents_ide.errors import AppError


class _VersionLike(Protocol):
    graph_json: str


def build_single_agent_graph(version: _VersionLike, spec: SingleAgentSpec) -> dict[str, Any]:
    """Keep the selected node's ID, prompt, timeout, retries and node overrides.

    A role can be reused by several nodes. Require an explicit node in that
    case instead of silently running the first task in the version.
    References to excluded steps are rejected by normal graph validation.
    """
    graph = json.loads(version.graph_json)
    matches = [
        node
        for node in graph["nodes"]
        if node["type"] in {"AgentTask", "LLMRequest"}
        and node.get("config", {}).get("role") == spec.role
        and (spec.node_id is None or node["id"] == spec.node_id)
    ]
    if not matches:
        raise AppError(
            "single_agent_role_unknown",
            "Выбранный узел и роль не соответствуют AgentTask/LLMRequest в версии",
            422,
        )
    if len(matches) != 1:
        raise AppError(
            "single_agent_node_required",
            "Роль используется в нескольких узлах; выберите node_id",
            422,
            {"node_ids": [node["id"] for node in matches]},
        )
    node = deepcopy(matches[0])
    context_refs = [
        ".".join(ref)
        for ref in template_references(node["config"]["prompt"])
        if ref[0] == "steps"
        or ref[:2] in {("work", "current_plan_item_id"), ("work", "plan_item_ids")}
    ]
    if node["config"].get("plan_check") or context_refs:
        raise AppError(
            "single_agent_context_required",
            "Узел требует результатов pipeline или состояния плана; запустите привязку целиком",
            422,
            {"node_id": node["id"], "references": context_refs},
        )
    if (isinstance(spec.selection, DirectAgentSelection) and node["type"] != "AgentTask") or (
        isinstance(spec.selection, DirectLLMSelection) and node["type"] != "LLMRequest"
    ):
        raise AppError(
            "single_agent_selection_mismatch", "Прямой выбор не соответствует типу узла", 422
        )
    if spec.selection is not None:
        node["config"]["model_selection"] = spec.selection.model_dump(mode="json")
    if spec.parameters is not None:
        node["config"]["params"] = {**node["config"].get("params", {}), **spec.parameters}
    start = "start" if node["id"] != "start" else "single_start"
    end = "end" if node["id"] != "end" else "single_end"
    return {
        "roles": {spec.role: "agent" if node["type"] == "AgentTask" else "llm"},
        "nodes": [{"id": start, "type": "Start"}, node, {"id": end, "type": "End"}],
        "edges": [
            {"id": "e1", "from": start, "to": node["id"]},
            {"id": "e2", "from": node["id"], "to": end},
        ],
        "input_schema": deepcopy(graph.get("input_schema", {})),
    }


def filter_single_agent_configuration(
    configuration: dict[str, Any],
    sources: dict[str, Any],
    spec: SingleAgentSpec,
    graph: dict[str, Any],
) -> None:
    """Pin only the effective choice, including a higher-priority node choice."""
    for key in ("model_selections", "model_overrides", "role_assignments", "role_parameters"):
        configuration[key] = {
            role: value for role, value in configuration[key].items() if role == spec.role
        }
        prefix = f"{key}."
        for name in list(sources):
            if name.startswith(prefix):
                tail = name[len(prefix) :]
                if tail != spec.role and not (
                    key == "role_parameters" and tail.startswith(f"{spec.role}.")
                ):
                    sources.pop(name)
    selection = graph["nodes"][1]["config"].get("model_selection")
    if selection is not None:
        configuration["model_selections"] = {spec.role: selection}
        sources["model_selections"] = "run" if spec.selection else "node"
        sources[f"model_selections.{spec.role}"] = "run" if spec.selection else "node"
    if configuration["model_selections"]:
        # Typed choices replace legacy endpoint/model fields together.
        for key in ("model_overrides", "role_assignments"):
            configuration[key] = {}
            sources[key] = "default"
            sources.pop(f"{key}.{spec.role}", None)
    # The reduced graph has no command nodes. Its inherited pipeline filter
    # must not prevent a standalone task; explicit invalid IDs still fail preflight.
    if sources["command_filter"] != "run":
        configuration["command_filter"] = []
        sources["command_filter"] = "default"
