"""Resolve settings once: defaults < pipeline version < binding < Run < node."""

import json
from copy import deepcopy
from typing import Any, Literal

from sqlalchemy.orm import Session

from agents_ide.domain.common import content_hash
from agents_ide.domain.schemas import SettingsOverrides
from agents_ide.errors import AppError
from agents_ide.persistence.models import (
    HarnessProfile,
    PipelineBinding,
    PipelineVersion,
    ProviderConnection,
)

DEFAULTS: dict[str, Any] = {
    "branch_policy": "run_branch",
    "dirty_policy": "strict",
    "role_assignments": {},
    "model_overrides": {},
    "limit_overrides": {"max_calls": 100},
    "command_filter": [],
}
SettingSource = Literal["run", "binding", "template", "node", "default"]


def resolve_configuration(
    binding: PipelineBinding,
    version: PipelineVersion,
    overrides: SettingsOverrides | None = None,
) -> tuple[dict[str, Any], dict[str, SettingSource]]:
    result = deepcopy(DEFAULTS)
    sources: dict[str, SettingSource] = dict.fromkeys(result, "default")
    for name, default_value in result.items():
        if isinstance(default_value, dict):
            for key in default_value:
                sources[f"{name}.{key}"] = "default"
    explicit_binding = json.loads(binding.settings_json)
    # Upgrade compatibility: non-default pre-review fields were explicit settings.
    if not explicit_binding:
        for key, default in DEFAULTS.items():
            value = (
                getattr(binding, key)
                if key.endswith("policy")
                else json.loads(getattr(binding, key + "_json"))
            )
            if value != default:
                explicit_binding[key] = value
    layers: list[tuple[SettingSource, dict[str, Any]]] = [
        ("template", json.loads(version.settings_json)),
        ("binding", explicit_binding),
        ("run", overrides.model_dump(exclude_none=True) if overrides else {}),
    ]
    for source, layer in layers:
        for key, value in layer.items():
            if value is None:
                continue
            if isinstance(value, dict):
                if not value:
                    continue
                result[key] = {**result[key], **value}
                for subkey in value:
                    sources[f"{key}.{subkey}"] = source
            else:
                result[key] = value
            sources[key] = source
    return result, sources


def policy_hash(configuration: dict[str, Any]) -> str:
    return content_hash(configuration)


def capture_dependencies(
    session: Session,
    graph: dict[str, Any],
    configuration: dict[str, Any],
) -> dict[str, Any]:
    """Pin profile metadata and opaque SecretStore refs, never secret values."""
    profiles: dict[str, Any] = {}
    providers: dict[str, Any] = {}
    nodes: dict[str, Any] = {}
    for node in graph.get("nodes", []):
        if not isinstance(node, dict):
            continue  # Full graph validation is stage 3.
        config = deepcopy(node.get("config", {}))
        if not isinstance(config, dict):
            raise AppError("configuration_invalid", "Настройки узла должны быть объектом", 409)
        role = config.get("role")
        if role is not None and not isinstance(role, str):
            raise AppError("configuration_invalid", "Роль узла должна быть строкой", 409)
        profile_id = configuration["role_assignments"].get(role)
        if profile_id:
            profile = session.get(HarnessProfile, profile_id)
            if profile is None or profile.archived_at is not None:
                raise AppError("harness_unavailable", "Профиль роли недоступен", 409)
            profiles[profile_id] = {
                "id": profile.id,
                "version": profile.version,
                "harness_kind": profile.harness_kind,
                "executable_path": profile.executable_path,
                "settings": json.loads(profile.settings_json),
            }
            config = {
                **json.loads(profile.settings_json),
                **(
                    {"model": configuration["model_overrides"][role]}
                    if role in configuration["model_overrides"]
                    else {}
                ),
                **config,
            }
            config["harness_profile_id"] = profile_id
        connection_id = config.get("connection_id")
        if connection_id is not None and not isinstance(connection_id, str):
            raise AppError("configuration_invalid", "ID подключения должен быть строкой", 409)
        if connection_id:
            connection = session.get(ProviderConnection, connection_id)
            if connection is None or connection.archived_at is not None:
                raise AppError("connection_unavailable", "Подключение недоступно", 409)
            providers[connection_id] = {
                "id": connection.id,
                "version": connection.version,
                "provider_kind": connection.provider_kind,
                "base_url": connection.base_url,
                "protocol": connection.protocol,
                "secret_reference": connection.secret_reference,
            }
        if "id" in node:
            if not isinstance(node["id"], str):
                raise AppError("configuration_invalid", "ID узла должен быть строкой", 409)
            nodes[node["id"]] = config
    return {"harness_profiles": profiles, "provider_connections": providers, "nodes": nodes}
