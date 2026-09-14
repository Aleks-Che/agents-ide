"""Read-only preflight; network calls and repository mutations are never probes."""

from __future__ import annotations

import json
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from sqlalchemy.orm import Session

from agents_ide.domain.graph_ast import ASTError, ASTNode, EvaluationContext, Value, evaluate
from agents_ide.domain.graph_validation import (
    ValidationIssue,
    ValidationReport,
    check_relative_path,
    check_version_features,
    validate_graph,
    validate_parameters,
)
from agents_ide.domain.schemas import SettingsOverrides, _reject_credentials
from agents_ide.domain.workspace import GitMetadata, collect_workspace
from agents_ide.errors import AppError
from agents_ide.persistence.models import (
    HarnessProfile,
    PipelineBinding,
    PipelineVersion,
    Project,
    ProviderConnection,
)
from agents_ide.services.settings import capture_dependencies, execution_hash, resolve_configuration


def preflight(
    session: Session,
    binding: PipelineBinding,
    *,
    inputs: dict[str, Any] | None = None,
    overrides: SettingsOverrides | None = None,
    execution_mode: str = "real",
    fake_scenario: dict[str, Any] | None = None,
    workspace_state: tuple[str, str, int, int, GitMetadata | None] | None = None,
) -> ValidationReport:
    version = session.get(PipelineVersion, binding.version_id)
    project = session.get(Project, binding.project_id)
    if version is None or project is None:
        raise AppError("not_found", "Версия или проект не найдены", 404)
    graph = json.loads(version.graph_json)
    values = {**json.loads(version.inputs_json), **(inputs or {})}
    report = validate_graph(graph, inputs=values)
    check_version_features(
        version.schema_version, json.loads(version.required_features_json), report
    )
    if not report.ok:
        return report
    try:
        _reject_credentials(values)
    except ValueError:
        report.add_error(
            ValidationIssue(
                "inputs_credentials_forbidden", "Секреты должны храниться в SecretStore"
            )
        )
        return report
    if binding.archived_at is not None or project.archived_at is not None:
        report.add_error(
            ValidationIssue("configuration_archived", "Привязка или проект архивированы")
        )
    for error in Draft202012Validator(graph.get("input_schema", {})).iter_errors(values):
        report.add_error(
            ValidationIssue(
                "input_invalid",
                "Обязательные входы отсутствуют или имеют неверный тип",
                details={"path": list(error.path), "rule": error.validator},
            )
        )
    configuration, sources = resolve_configuration(binding, version, overrides)
    limit_caps = {
        "max_calls": 10000,
        "max_node_visits": 1000,
        "max_backward_transitions": 200,
        "max_duration_seconds": 86400,
    }
    for key, limit in configuration["limit_overrides"].items():
        if (
            key not in limit_caps
            or not float(limit).is_integer()
            or not 0 < limit <= limit_caps[key]
        ):
            report.add_error(
                ValidationIssue(
                    "budget_unsupported",
                    "Неизвестный или неподдержанный строгий лимит",
                    details={"limit": key},
                )
            )
    report.preview = {
        "resolved_settings": configuration,
        "setting_sources": sources,
        "inputs": values,
        "candidates": {},
        "commands": [],
        "data_destinations": [],
        "permissions": {"status": "unverified", "autonomous_write": "blocked"},
        "dispatch_ready": False,
        "mutable_checks_required_before_dispatch": True,
        "requires_trust": getattr(version, "origin", "local") == "imported",
    }
    report.add_warning(
        ValidationIssue(
            "runtime_unimplemented",
            "Исполнение и повторная проверка под резервацией относятся к этапам 4–8",
        )
    )
    if configuration["dirty_policy"] == "allow_nonoverlap":
        report.add_error(
            ValidationIssue(
                "policy_unsupported", "allow_nonoverlap требует подтверждённой изоляции этапа 8"
            )
        )
    try:
        dependencies = capture_dependencies(session, graph, configuration)
    except AppError as exc:
        report.add_error(ValidationIssue(exc.code, exc.message, details=exc.details or {}))
        return report
    report.execution_hash = execution_hash(
        version,
        configuration,
        dependencies,
        values,
        execution_mode=execution_mode,
        fake_scenario=fake_scenario,
    )
    report.preview["execution_mode"] = execution_mode
    report.preview["simulated"] = execution_mode == "simulated"
    report.features = sorted(set(report.features) | set(json.loads(version.required_features_json)))
    if dependencies["model_groups"]:
        report.features = sorted({*report.features, "model_groups"})
    if getattr(version, "origin", "local") == "imported":
        report.add_warning(
            ValidationIssue(
                "import_trust_required", "Run требует trusted_execution_hash из этого preflight"
            )
        )
    workspace = Path(project.workspace_normalized_path)
    try:
        _, normalized, dev, ino, git = workspace_state or collect_workspace(
            project.workspace_entered_path
        )
        workspace = Path(normalized)
        if (dev, ino) != (project.workspace_identity_dev, project.workspace_identity_ino):
            report.add_error(
                ValidationIssue("workspace_conflict", "Идентичность рабочего каталога изменилась")
            )
        report.preview["git_plan"] = {
            "branch_policy": configuration["branch_policy"],
            "dirty_policy": configuration["dirty_policy"],
            "repository": git.root_path if git else None,
            "head": git.head_sha if git else None,
            "branch": git.default_branch if git else None,
            "dirty": git.dirty if git else False,
            "will_switch_branch": bool(git and configuration["branch_policy"] == "run_branch"),
            "hooks_and_baseline": "unverified",
            "mutations_performed": False,
        }
        if any(n["type"] == "GitCommit" for n in graph["nodes"]):
            if git is None or not git.head_sha:
                report.add_error(
                    ValidationIssue(
                        "git_repository_required", "GitCommit требует репозиторий с HEAD"
                    )
                )
            elif git.dirty:
                report.add_error(
                    ValidationIssue(
                        "git_dirty", "GitCommit требует проверки чистого исходного состояния"
                    )
                )
            elif configuration["branch_policy"] == "current" and git.default_branch is None:
                report.add_error(
                    ValidationIssue("git_detached_head", "current не поддерживает detached HEAD")
                )
    except AppError as exc:
        report.add_error(ValidationIssue(exc.code, exc.message))
    all_command_ids: set[str] = set()
    for node in graph["nodes"]:
        node_id = node["id"]
        config = dependencies["nodes"][node_id]
        if node["type"] in ("AgentTask", "LLMRequest"):
            _candidates(session, node, config, dependencies, configuration, report)
        if node["type"] == "Command":
            commands = config["commands"]
            if isinstance(commands, dict):
                try:
                    ctx = EvaluationContext(inputs={k: Value.of(v) for k, v in values.items()})
                    commands = evaluate(ASTNode.from_json(commands), ctx).raw
                    # Reuse the same command schema and semantic validation after resolving input.
                    resolved_graph = deepcopy(graph)
                    next(n for n in resolved_graph["nodes"] if n["id"] == node_id)["config"][
                        "commands"
                    ] = commands
                    resolved_report = validate_graph(resolved_graph, inputs=values)
                    if not resolved_report.ok:
                        raise ASTError("Invalid command input")
                except (ASTError, ValueError, TypeError):
                    report.add_error(
                        ValidationIssue(
                            "command_input_invalid",
                            "Вход не содержит допустимый список команд",
                            node_id,
                        )
                    )
                    continue
            all_command_ids.update(c["id"] for c in commands)
            for command in commands:
                program = command["program"]
                resolved = shutil.which(program)
                if resolved is None:
                    report.add_error(
                        ValidationIssue(
                            "command_unavailable",
                            "Программа команды не найдена",
                            node_id,
                            details={"command_id": command["id"]},
                        )
                    )
                try:
                    cwd = _local_path(workspace, command.get("cwd", "."))
                    if not cwd.is_dir():
                        raise ASTError("cwd not available")
                except (ASTError, OSError, RuntimeError):
                    report.add_error(
                        ValidationIssue(
                            "command_cwd_invalid",
                            "cwd команды недоступен или выходит за workspace",
                            node_id,
                        )
                    )
                report.preview["commands"].append(
                    {"node_id": node_id, **command, "resolved_program": resolved}
                )
        if node["type"] == "CollectContext":
            paths = config.get("context_paths", []) + [
                s["path"] for s in config.get("sources", []) if "path" in s
            ]
            for path_text in paths:
                try:
                    _local_path(workspace, path_text)
                except (ASTError, OSError, RuntimeError):
                    report.add_error(
                        ValidationIssue(
                            "context_path_invalid",
                            "Путь контекста выходит за разрешённую область",
                            node_id,
                        )
                    )
    if set(configuration["command_filter"]) - all_command_ids:
        report.add_error(
            ValidationIssue("command_filter_invalid", "Фильтр содержит неизвестный ID команды")
        )
    if execution_mode == "simulated":
        unsupported = [
            n["id"]
            for n in graph["nodes"]
            if n["type"] not in {"Start", "End", "Condition", "AgentTask", "LLMRequest"}
        ]
        if unsupported:
            report.add_error(
                ValidationIssue(
                    "node_executor_unimplemented",
                    "Исполнитель узла ожидает следующих этапов",
                    details={"node_ids": unsupported},
                )
            )
        for response in (fake_scenario or {}).get("responses", []):
            if response["node_id"] not in {
                n["id"] for n in graph["nodes"] if n["type"] in {"AgentTask", "LLMRequest"}
            }:
                report.add_error(
                    ValidationIssue(
                        "fake_node_unknown", "Сценарий ссылается на неизвестного исполнителя"
                    )
                )
        report.preview["dispatch_ready"] = report.ok
        report.preview["permissions"] = {
            "status": "simulated",
            "network": False,
            "writes": "private_test_workspace",
        }
        report.preview["data_destinations"] = [{"kind": "local_simulation", "network": False}]
        report.warnings = [
            w
            for w in report.warnings
            if w.code not in {"runtime_unimplemented", "capabilities_unverified"}
        ]
    elif fake_scenario is not None:
        report.add_error(
            ValidationIssue("simulation_mode_required", "Сценарий требует явного simulated-режима")
        )
    return report


def _local_path(workspace: Path, text: str) -> Path:
    check_relative_path(text)
    target = (workspace / text).resolve()
    if not target.is_relative_to(workspace.resolve()):
        raise ASTError("Path escaped workspace")
    return target


def _candidates(
    session: Session,
    node: dict[str, Any],
    config: dict[str, Any],
    dependencies: dict[str, Any],
    configuration: dict[str, Any],
    report: ValidationReport,
) -> None:
    node_id = node["id"]
    agent = node["type"] == "AgentTask"
    candidates = config.get("candidates")
    if candidates is None:
        ref = config.get("harness_profile_id" if agent else "connection_id")
        model = config.get("model")
        if not ref or not model:
            report.add_error(
                ValidationIssue(
                    "model_selection_missing",
                    "Роль или узел не выбирает исполнителя и модель",
                    node_id,
                )
            )
            return
        candidates = [
            {
                "id": None,
                "member_index": 0,
                "enabled": True,
                "model_id": model,
                "harness_profile_id": ref if agent else None,
                "provider_connection_id": None if agent else ref,
                "params": config.get("params", {}),
                "parameter_sources": {},
            }
        ]
    rows = []
    usable = 0
    role = node.get("config", {}).get("role")
    choice_source = (
        "node"
        if "model_selection" in node.get("config", {})
        else report.preview["setting_sources"].get(f"model_selections.{role}", "legacy_direct")
    )
    for candidate in candidates:
        row = deepcopy(candidate)
        row["selection_source"] = choice_source
        reason = candidate.get("unavailable_reason")
        if not candidate["enabled"]:
            reason = "disabled"
        ref = candidate["harness_profile_id" if agent else "provider_connection_id"]
        resource = (
            session.get(HarnessProfile, ref) if agent else session.get(ProviderConnection, ref)
        )
        try:
            validate_parameters(candidate["params"])
        except (ValueError, TypeError):
            report.add_error(
                ValidationIssue(
                    "model_params_invalid",
                    "Неподдержанные параметры модели",
                    node_id,
                    details={"member_id": candidate["id"]},
                )
            )
        row["capabilities"] = {
            "status": "unverified",
            "context_window": "unknown",
            "response_format": "unverified",
            "requested_response_format": config.get("response_format", "text"),
            "budget_telemetry": "unknown",
            "permissions": "unverified",
        }
        if resource is None:
            reason = reason or "missing"
        elif resource.archived_at is not None:
            reason = reason or "archived"
        elif isinstance(resource, HarnessProfile):
            if resource.executable_path and shutil.which(resource.executable_path) is None:
                reason = reason or "executable_unavailable"
            row["destination"] = {
                "kind": "harness",
                "profile_id": resource.id,
                "harness_kind": resource.harness_kind,
                "provider_destination": "unknown",
            }
        else:
            row["destination"] = {
                "kind": "llm",
                "connection_id": resource.id,
                "base_url": resource.base_url,
            }
            row["catalog"] = {
                "fetched_at": resource.catalog_fetched_at,
                "ttl_seconds": resource.catalog_ttl_seconds,
                "availability": "unverified",
            }
            if resource.secret_reference and candidate["enabled"]:
                store = session.info.get("secret_store")
                if store is not None:
                    try:
                        store.get(resource.secret_reference)
                    except AppError:
                        reason = reason or "secret_unavailable"
                else:
                    reason = reason or "secret_unavailable"
        if reason is None and candidate["enabled"]:
            usable += 1
        row["status"] = (
            "configured"
            if reason is None
            else "disabled"
            if reason == "disabled"
            else "unavailable"
        )
        row["reason"] = reason
        row["budget"] = configuration["limit_overrides"]
        # Secret reference itself is internal snapshot data, never a preflight destination.
        row.pop("secret_reference", None)
        rows.append(row)
        if "destination" in row:
            report.preview["data_destinations"].append(
                {"node_id": node_id, "member_id": row["id"], **row["destination"]}
            )
        if reason and candidate["enabled"]:
            report.add_warning(
                ValidationIssue(
                    "model_group_candidate_unavailable",
                    "Кандидат недоступен",
                    node_id,
                    details={"member_id": row["id"], "reason": reason},
                )
            )
    report.preview["candidates"][node_id] = rows
    if not usable:
        report.add_error(
            ValidationIssue(
                "model_group_unavailable" if config.get("model_group_id") else "model_unavailable",
                "Нет доступных настроенных кандидатов",
                node_id,
            )
        )
    else:
        report.add_warning(
            ValidationIssue(
                "capabilities_unverified",
                "Возможности модели требуют проверки адаптером перед dispatch",
                node_id,
            )
        )
