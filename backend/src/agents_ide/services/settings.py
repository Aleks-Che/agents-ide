"""Resolve complete choices and pin all candidates before a Run is queued."""

import json
from copy import deepcopy
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy.orm import Session

from agents_ide.domain.common import content_hash
from agents_ide.domain.schemas import (
    MODEL_SELECTION,
    DirectAgentSelection,
    GroupSelection,
    ModelSelection,
    SettingsOverrides,
)
from agents_ide.errors import AppError
from agents_ide.persistence.models import (
    HarnessProfile,
    PipelineBinding,
    PipelineVersion,
    ProviderConnection,
)
from agents_ide.services.groups import load_group_snapshot

DEFAULTS: dict[str, Any] = {
    "branch_policy": "run_branch",
    "dirty_policy": "strict",
    "role_assignments": {},
    "model_selections": {},
    "model_overrides": {},
    "role_parameters": {},
    "limit_overrides": {"max_calls": 100},
    "command_filter": [],
}
SettingSource = Literal["run", "binding", "template", "node", "default"]


def resolve_configuration(
    binding: PipelineBinding, version: PipelineVersion, overrides: SettingsOverrides | None = None
) -> tuple[dict[str, Any], dict[str, SettingSource]]:
    result = deepcopy(DEFAULTS)
    sources: dict[str, SettingSource] = dict.fromkeys(result, "default")
    sources["limit_overrides.max_calls"] = "default"
    explicit = json.loads(binding.settings_json)
    if not explicit:
        for key, default in DEFAULTS.items():
            raw = getattr(binding, key if key.endswith("policy") else key + "_json", None)
            value = raw if key.endswith("policy") else json.loads(raw or "{}")
            if raw is not None and value != default:
                explicit[key] = value
    if binding.model_selections_json:
        explicit.setdefault("model_selections", json.loads(binding.model_selections_json))
    layers: list[tuple[SettingSource, dict[str, Any]]] = [
        ("template", json.loads(version.settings_json)),
        ("binding", explicit),
        ("run", overrides.model_dump(mode="json", exclude_none=True) if overrides else {}),
    ]
    for source, layer in layers:
        # Both channels represent the same choice: the higher layer replaces it.
        for key, opposite in [
            ("model_selections", "model_overrides"),
            ("model_overrides", "model_selections"),
        ]:
            for role in layer.get(key) or {}:
                result[opposite].pop(role, None)
                sources.pop(f"{opposite}.{role}", None)
        for key, value in layer.items():
            if value is None or value == {}:
                continue
            if isinstance(value, dict):
                if key == "role_parameters":
                    for role, params in value.items():
                        result[key][role] = {**result[key].get(role, {}), **params}
                        for param in params:
                            sources[f"{key}.{role}.{param}"] = source
                else:
                    result[key] = {**result[key], **value}
                for subkey in value:
                    sources[f"{key}.{subkey}"] = source
            else:
                result[key] = value
            sources[key] = source
    # Never let Python/Pydantic objects leak into JSON snapshots or hashes.
    result["model_selections"] = {
        role: _selection(raw).model_dump(mode="json")
        for role, raw in result["model_selections"].items()
    }
    return result, sources


def _selection(raw: Any) -> ModelSelection:
    try:
        # Original 0004 serialized optional null fields. They were never choices.
        if isinstance(raw, dict):
            raw = {key: value for key, value in raw.items() if value is not None}
        return MODEL_SELECTION.validate_python(raw)
    except ValidationError:
        raise AppError("configuration_invalid", "Неверный выбор модели", 422) from None


def policy_hash(configuration: dict[str, Any]) -> str:
    return content_hash(configuration)


def execution_hash(
    version: PipelineVersion,
    configuration: dict[str, Any],
    dependencies: dict[str, Any],
    inputs: dict[str, Any] | None = None,
    *,
    execution_mode: str = "real",
    fake_scenario: dict[str, Any] | None = None,
    graph: dict[str, Any] | None = None,
    single_agent_role: str | None = None,
) -> str:
    payload: dict[str, Any] = {
        "pipeline_execution_hash": version.execution_hash,
        "configuration": configuration,
        "dependencies": dependencies,
        "inputs": json.loads(version.inputs_json) if inputs is None else inputs,
        **(
            {"execution_mode": execution_mode, "fake_scenario": fake_scenario}
            if execution_mode == "simulated"
            else {}
        ),
    }
    if graph is not None:
        from agents_ide.domain.graph_validation import executable_payload

        payload["graph"] = executable_payload(graph)
    if single_agent_role is not None:
        payload["single_agent_role"] = single_agent_role
    return content_hash(payload)


def capture_dependencies(
    session: Session, graph: dict[str, Any], configuration: dict[str, Any]
) -> dict[str, Any]:
    try:
        return _capture_dependencies(session, graph, configuration)
    except (ValueError, TypeError):
        raise AppError(
            "configuration_invalid", "Неверные параметры модели или источника", 422
        ) from None


def _capture_dependencies(
    session: Session, graph: dict[str, Any], configuration: dict[str, Any]
) -> dict[str, Any]:
    from agents_ide.domain.graph_validation import validate_parameters

    profiles: dict[str, Any] = {}
    providers: dict[str, Any] = {}
    groups: dict[str, Any] = {}
    nodes: dict[str, Any] = {}

    def resource(kind: str, ref: str, *, grouped: bool = False) -> dict[str, Any]:
        row = (
            session.get(HarnessProfile, ref)
            if kind == "agent"
            else session.get(ProviderConnection, ref)
        )
        if row is None or (row.archived_at is not None and not grouped):
            raise AppError(
                "harness_unavailable" if kind == "agent" else "connection_unavailable",
                "Исполнитель недоступен",
                409,
            )
        common = {"id": row.id, "version": row.version, "archived": row.archived_at is not None}
        if isinstance(row, HarnessProfile):
            data = {
                **common,
                "harness_kind": row.harness_kind,
                "executable_path": row.executable_path,
                "settings": json.loads(row.settings_json),
            }
            profiles[ref] = data
        else:
            data = {
                **common,
                "provider_kind": row.provider_kind,
                "base_url": row.base_url,
                "protocol": row.protocol,
                "secret_reference": row.secret_reference,
            }
            providers[ref] = data
        return data

    def pin(selection: ModelSelection) -> tuple[str, list[dict[str, Any]]]:
        if isinstance(selection, GroupSelection):
            if selection.group_id not in groups:
                loaded = load_group_snapshot(session, selection.group_id)
                if loaded is None:
                    raise AppError("model_group_unavailable", "Группа недоступна", 409)
                model, members = loaded
                if not any(m.enabled for m in members):
                    raise AppError("model_group_empty", "Нужен включённый кандидат", 422)
                candidates = []
                for member in members:
                    ref = (
                        member.harness_profile_id
                        if model.kind == "agent"
                        else member.provider_connection_id
                    )
                    if not ref or (member.harness_profile_id is not None) != (
                        model.kind == "agent"
                    ):
                        raise AppError(
                            "configuration_invalid", "Тип кандидата не совпадает с группой", 422
                        )
                    data = resource(model.kind, ref, grouped=True)
                    params = json.loads(member.params_json)
                    validate_parameters(params)
                    candidates.append(
                        {
                            "id": member.id,
                            "member_index": member.member_index,
                            "enabled": member.enabled,
                            "harness_profile_id": member.harness_profile_id,
                            "provider_connection_id": member.provider_connection_id,
                            "model_id": member.model_id,
                            "params": params,
                            "revision": member.revision,
                            "resource_version": data["version"],
                            "unavailable_reason": "archived" if data["archived"] else None,
                        }
                    )
                groups[model.id] = {
                    "id": model.id,
                    "name": model.name,
                    "kind": model.kind,
                    "revision": model.revision,
                    "members": candidates,
                }
            group = groups[selection.group_id]
            return group["kind"], group["members"]
        agent = isinstance(selection, DirectAgentSelection)
        ref = (
            selection.harness_profile_id
            if isinstance(selection, DirectAgentSelection)
            else selection.provider_connection_id
        )
        kind = "agent" if agent else "llm"
        data = resource(kind, ref)
        return kind, [
            {
                "id": None,
                "member_index": 0,
                "enabled": True,
                "harness_profile_id": ref if agent else None,
                "provider_connection_id": None if agent else ref,
                "model_id": selection.model_id,
                "params": {},
                "resource_version": data["version"],
                "unavailable_reason": None,
            }
        ]

    selections = {
        role: _selection(raw) for role, raw in configuration.get("model_selections", {}).items()
    }
    # Validate even unused assignments; new bindings must not reference archived groups.
    for assigned in selections.values():
        pin(assigned)
    for node in graph.get("nodes", []):
        if not isinstance(node, dict):
            continue  # Full graph validation belongs to stage 3.
        config = deepcopy(node.get("config", {}))
        if not isinstance(config, dict) or not isinstance(node.get("id"), str):
            raise AppError("configuration_invalid", "Неверная структура узла", 422)
        role = config.get("role")
        if role is not None and not isinstance(role, str):
            raise AppError("configuration_invalid", "Роль должна быть строкой", 422)
        selection: ModelSelection | None = (
            _selection(config["model_selection"])
            if "model_selection" in config
            else selections.get(role)
            if role
            else None
        )
        node_type = node.get("type")
        expected_kind = (
            {"AgentTask": "agent", "LLMRequest": "llm"}.get(node_type)
            if isinstance(node_type, str)
            else None
        )
        if selection is not None:
            kind, candidates = pin(selection)
            if kind != expected_kind:
                raise AppError(
                    "model_group_kind_mismatch",
                    "Выбор не соответствует типу узла",
                    422,
                    {"node_id": node["id"], "expected_kind": expected_kind},
                )
            role_params = configuration.get("role_parameters", {}).get(role, {})
            node_params = config.get("params", {})
            resolved = []
            for candidate in candidates:
                profile = profiles.get(candidate["harness_profile_id"], {})
                defaults = deepcopy(profile.get("settings", {}).get("params", {}))
                # Compatibility with flat profile generation defaults.
                for key in (
                    "reasoning_effort",
                    "temperature",
                    "top_p",
                    "max_tokens",
                    "max_output_tokens",
                    "seed",
                ):
                    if key in profile.get("settings", {}):
                        defaults[key] = profile["settings"][key]
                params = {**defaults, **candidate["params"], **role_params, **node_params}
                validate_parameters(params)
                param_sources = {
                    key: "profile" if kind == "agent" else "connection" for key in defaults
                }
                for source, values in [
                    ("candidate", candidate["params"]),
                    ("role", role_params),
                    ("node", node_params),
                ]:
                    param_sources.update(dict.fromkeys(values, source))
                resolved.append({**candidate, "params": params, "parameter_sources": param_sources})
            # A typed selection replaces old model and endpoint fields together.
            for key in ("model", "connection_id", "harness_profile_id", "model_group_id"):
                config.pop(key, None)
            config["model_selection"] = selection.model_dump(mode="json")
            config["candidates"] = resolved
            if isinstance(selection, GroupSelection):
                config["model_group_id"] = selection.group_id
            else:
                config["model"] = selection.model_id
                config["params"] = resolved[0]["params"]
                if isinstance(selection, DirectAgentSelection):
                    config["harness_profile_id"] = selection.harness_profile_id
                else:
                    config["connection_id"] = selection.provider_connection_id
        else:
            # Original direct configuration remains a direct execution without fallback.
            profile_id = configuration["role_assignments"].get(role) or config.get(
                "harness_profile_id"
            )
            if profile_id:
                if not isinstance(profile_id, str):
                    raise AppError("configuration_invalid", "Invalid harness profile ID", 422)
                data = resource("agent", profile_id)
                config = {
                    **data["settings"],
                    **configuration.get("role_parameters", {}).get(role, {}),
                    **(
                        {"model": configuration["model_overrides"][role]}
                        if role in configuration["model_overrides"]
                        else {}
                    ),
                    **config,
                }
                config["harness_profile_id"] = profile_id
            connection_id = config.get("connection_id")
            if connection_id is not None:
                if not isinstance(connection_id, str):
                    raise AppError(
                        "configuration_invalid", "ID подключения должен быть строкой", 422
                    )
                resource("llm", connection_id)
                if role in configuration["model_overrides"]:
                    config["model"] = configuration["model_overrides"][role]
            if expected_kind:
                params = {
                    key: config[key]
                    for key in (
                        "reasoning_effort",
                        "temperature",
                        "top_p",
                        "max_tokens",
                        "max_output_tokens",
                        "seed",
                    )
                    if key in config
                }
                params.update(config.get("params", {}))
                validate_parameters(params)
                if params:
                    config["params"] = params
        nodes[node["id"]] = config
    return {
        "model_selection_version": 1,
        "harness_profiles": profiles,
        "provider_connections": providers,
        "model_groups": groups,
        "nodes": nodes,
    }
