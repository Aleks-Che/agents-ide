"""Capture current model-stage settings when a stage is explicitly restarted."""

import json
from typing import Any

from sqlalchemy.orm import Session

from agents_ide.domain.graph_validation import executable_payload
from agents_ide.domain.schemas import SettingsOverrides
from agents_ide.engine.run_configuration import effective_snapshot
from agents_ide.errors import AppError
from agents_ide.operations.storage import settings_for
from agents_ide.persistence.models import PipelineBinding, PipelineVersion, Run
from agents_ide.services.settings import (
    capture_dependencies,
    execution_hash,
    policy_hash,
    resolve_configuration,
)


def _structure(graph: dict[str, Any]) -> dict[str, Any]:
    body = executable_payload(graph)
    for node in body["nodes"]:
        if node["type"] in {"AgentTask", "LLMRequest"}:
            # Prompts, model selection and JSON processing are stage settings.
            # Plan verification changes need a fresh preflight.
            node["config"] = {
                key: value for key, value in node.get("config", {}).items() if key == "plan_check"
            }
    return body


def capture_stage_configuration(session: Session, run: Run) -> dict[str, Any] | None:
    snapshot = effective_snapshot(run)
    if snapshot.get("single_agent"):
        return None  # Its task is a synthetic graph, not an editable template stage.
    binding = session.get(PipelineBinding, run.binding_id)
    definition = session.get(PipelineVersion, binding.version_id) if binding else None
    if definition is None or binding is None:
        raise AppError("template_not_found", "Сохранённый шаблон недоступен", 409)
    graph = json.loads(definition.graph_json)
    if _structure(graph) != _structure(snapshot["graph"]):
        raise AppError(
            "stage_restart_unavailable",
            "Связи или операции шаблона изменились. Запустите шаблон заново.",
            409,
        )
    request = json.loads(run.request_json or "{}")
    settings, sources = resolve_configuration(
        binding, definition, SettingsOverrides.model_validate(request.get("overrides", {}))
    )
    if not settings_for(session).enforce_execution_limits:
        settings["limit_overrides"] = {}
    for key in ("workspace_mode", "branch_policy", "dirty_policy", "command_filter"):
        if settings.get(key) != snapshot["resolved_settings"].get(key):
            raise AppError(
                "stage_restart_unavailable",
                "Настройки рабочей области изменились. Запустите шаблон заново.",
                409,
            )
    dependencies = capture_dependencies(session, graph, settings)
    for key in ("command_programs", "git"):
        if key in snapshot["dependencies"]:
            dependencies[key] = snapshot["dependencies"][key]
    digest = execution_hash(
        definition,
        settings,
        dependencies,
        snapshot["input"]["values"],
        execution_mode=snapshot.get("execution_mode", "real"),
        fake_scenario=snapshot.get("fake_scenario"),
    )
    if definition.origin == "imported" and digest != snapshot["execution_hash"]:
        raise AppError(
            "import_trust_required", "Подтвердите изменённый импорт при новом запуске", 409
        )
    return {
        "graph": graph,
        "dependencies": dependencies,
        "schema_version": definition.schema_version,
        "required_features": json.loads(definition.required_features_json),
        "resolved_settings": settings,
        "setting_sources": sources,
        "execution_hash": digest,
        "policy_hash": policy_hash({**settings, **dependencies}),
        "pipeline_execution_hash": definition.execution_hash,
        "origin": definition.origin,
    }
