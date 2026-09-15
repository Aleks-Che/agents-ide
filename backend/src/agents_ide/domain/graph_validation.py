"""Bounded, deterministic validation of executable graphs and portable definitions."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from sqlalchemy.orm import Session

from agents_ide.domain.common import content_hash
from agents_ide.domain.graph_ast import (
    ASTError,
    ASTNode,
    EvaluationContext,
    Op,
    evaluate_truth,
    references,
    template_references,
    validate_reference,
)
from agents_ide.domain.graph_schema import (
    GRAPH_LIMITS,
    SUPPORTED_FEATURES,
    SUPPORTED_SCHEMA_VERSION,
    edge_endpoints,
    is_valid_node_id,
    node_schemas,
    required_features_for,
)
from agents_ide.domain.schemas import SettingsOverrides, _reject_credentials, validate_model_params
from agents_ide.domain.workspace import GitMetadata
from agents_ide.errors import AppError
from agents_ide.persistence.models import PipelineBinding, PipelineVersion


@dataclass
class ValidationIssue:
    code: str
    message: str
    node_id: str | None = None
    edge_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value for key, value in vars(self).items() if value is not None and value != {}
        }


@dataclass
class ValidationReport:
    ok: bool = True
    errors: list[ValidationIssue] = field(default_factory=list)
    warnings: list[ValidationIssue] = field(default_factory=list)
    graph_hash: str | None = None
    execution_hash: str | None = None
    features: list[str] = field(default_factory=list)
    preview: dict[str, Any] = field(default_factory=dict)

    def add_error(self, issue: ValidationIssue) -> None:
        self.errors.append(issue)
        self.ok = False

    def add_warning(self, issue: ValidationIssue) -> None:
        self.warnings.append(issue)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": [i.to_dict() for i in self.errors],
            "warnings": [i.to_dict() for i in self.warnings],
            "graph_hash": self.graph_hash,
            "execution_hash": self.execution_hash,
            "features": self.features,
            **self.preview,
        }


def check_version_features(version: Any, features: Any, report: ValidationReport) -> None:
    if version != SUPPORTED_SCHEMA_VERSION:
        report.add_error(
            ValidationIssue("schema_unsupported", "Поддерживается schema_version=1.0.0")
        )
    if not isinstance(features, list) or any(not isinstance(f, str) for f in features):
        report.add_error(
            ValidationIssue("features_invalid", "required_features должен быть списком строк")
        )
    elif set(features) - SUPPORTED_FEATURES:
        report.add_error(
            ValidationIssue("features_unsupported", "Требуется неподдержанная возможность")
        )


def _bounded(value: Any) -> bool:
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 64:
            return False
        if isinstance(item, dict):
            pending.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            pending.extend((v, depth + 1) for v in item)
    try:
        return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) <= 1048576
    except (ValueError, TypeError, RecursionError):
        return False


def check_data_schema(schema: Any) -> None:
    """Bounded local subset: never retrieve a schema URL or allow recursive references."""
    allowed = {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "description",
    }
    pending = [schema]
    while pending:
        item = pending.pop()
        if not isinstance(item, dict) or set(item) - allowed:
            raise ASTError("Unsupported data schema keyword")
        if isinstance(item.get("additionalProperties"), dict):
            pending.append(item["additionalProperties"])
        if "items" in item:
            pending.append(item["items"])
        if isinstance(item.get("properties"), dict):
            pending.extend(item["properties"].values())
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise ASTError("Invalid data schema") from None


def validate_graph(
    graph: Any, *, inputs: Mapping[str, Any] | None = None, allow_incomplete: bool = True
) -> ValidationReport:
    # Both modes report all blockers. Draft persistence deliberately does not require ok.
    report = ValidationReport()
    if not isinstance(graph, dict) or not _bounded(graph):
        report.add_error(
            ValidationIssue("graph_invalid", "Нужен JSON-объект до 1 MiB, глубина до 64")
        )
        return report
    if not all(isinstance(graph.get(k), list) for k in ("nodes", "edges")):
        report.add_error(ValidationIssue("graph_invalid", "nodes и edges должны быть массивами"))
        return report
    if len(graph["nodes"]) > 200 or len(graph["edges"]) > 400:
        report.add_error(ValidationIssue("graph_limit", "Превышен лимит 200 узлов / 400 связей"))
        return report
    if any(not isinstance(v, dict) for k in ("nodes", "edges") for v in graph[k]):
        report.add_error(ValidationIssue("graph_invalid", "Узлы и связи должны быть объектами"))
        return report
    catalog = node_schemas()
    schema = catalog["graph"]
    # Validate individual node schemas for useful node-specific diagnostics instead of oneOf noise.
    structure = deepcopy(schema)
    structure["properties"]["nodes"]["items"] = {"type": "object"}
    for error in Draft202012Validator(structure).iter_errors(graph):
        path = list(error.path)
        edge_id = None
        if len(path) >= 2 and path[0] == "edges":
            edge_id = _edge_id(graph["edges"][path[1]], path[1])
        report.add_error(
            ValidationIssue(
                "graph_schema_invalid",
                "Поле не соответствует схеме",
                edge_id=edge_id,
                details={"path": path, "rule": error.validator},
            )
        )
    nodes: dict[str, Any] = {}
    for node in graph["nodes"]:
        node_id = node.get("id")
        if not isinstance(node_id, str) or not is_valid_node_id(node_id):
            report.add_error(ValidationIssue("node_id_invalid", "Неверный ID узла"))
            continue
        if node_id in nodes:
            report.add_error(ValidationIssue("node_id_duplicate", "ID узла повторяется", node_id))
        nodes[node_id] = node
        kind = node.get("type")
        if not isinstance(kind, str) or kind not in catalog["nodes"]:
            report.add_error(ValidationIssue("node_type_unknown", "Неизвестный тип узла", node_id))
            continue
        node_schema = {**catalog["nodes"][kind], "components": catalog["components"]}
        for error in Draft202012Validator(node_schema).iter_errors(node):
            report.add_error(
                ValidationIssue(
                    "node_schema_invalid",
                    "Параметры узла не соответствуют схеме",
                    node_id,
                    details={"path": list(error.path), "rule": error.validator},
                )
            )
    starts = [n for n in nodes if nodes[n].get("type") == "Start"]
    ends = [n for n in nodes if nodes[n].get("type") == "End"]
    if len(starts) != 1:
        report.add_error(
            ValidationIssue(
                "start_missing" if not starts else "start_multiple", "Нужен ровно один Start"
            )
        )
    if not ends:
        report.add_error(ValidationIssue("end_missing", "Нужен End"))
    if report.errors:
        return report
    input_schema = graph.get("input_schema", {})
    try:
        check_data_schema(input_schema)
        if input_schema and input_schema.get("type") != "object":
            raise ASTError("Input schema must describe an object")
    except ASTError as exc:
        report.add_error(ValidationIssue("input_schema_invalid", str(exc)))
        return report
    names = set(input_schema.get("properties", {})) | set(inputs or {})
    for node_id, node in nodes.items():
        try:
            _validate_node_content(node, nodes, names, input_schema)
            role = node.get("config", {}).get("role")
            if role and graph.get("roles"):
                expected = "agent" if node["type"] == "AgentTask" else "llm"
                if graph["roles"].get(role) != expected:
                    raise ASTError("Role kind mismatch")
        except (ASTError, ValueError, TypeError):
            report.add_error(
                ValidationIssue(
                    "condition_reference_unknown"
                    if node["type"] == "Condition"
                    else "node_configuration_invalid",
                    "Неверная ссылка, выражение или параметры узла",
                    node_id,
                )
            )
    edges = graph["edges"]
    edge_ids = set()
    for i, edge in enumerate(edges):
        edge_id = _edge_id(edge, i)
        if edge_id in edge_ids:
            report.add_error(
                ValidationIssue("edge_id_duplicate", "ID связи повторяется", edge_id=edge_id)
            )
        edge_ids.add(edge_id)
        source, target, _ = edge_endpoints(edge)
        if source not in nodes or target not in nodes:
            report.add_error(
                ValidationIssue(
                    "edge_endpoint_unknown", "Связь ссылается на неизвестный узел", edge_id=edge_id
                )
            )
        for name, expression in edge.get("assignments", {}).items():
            try:
                ast = _check_ast(expression, nodes, names, input_schema)
                if name == "work.mode" and (
                    ast.op != Op.CONST or ast.value not in ("initial", "repair", "next_item")
                ):
                    raise ASTError("Invalid mode")
            except ASTError:
                report.add_error(
                    ValidationIssue(
                        "assignment_invalid", "Недопустимое присваивание перехода", edge_id=edge_id
                    )
                )
    if not report.errors:
        _connectivity(nodes, edges, starts[0], ends, report)
    report.graph_hash = content_hash(executable_payload(graph))
    report.features = required_features_for(graph)
    return report


def _edge_id(edge: dict[str, Any], index: int) -> str:
    return str(edge.get("id") or edge.get("edge_id") or f"edge_{index}")


def _reference_type(
    path: tuple[str, ...], nodes: Mapping[str, Any], schema: dict[str, Any]
) -> str | None:
    if path[0] == "steps" and path[3] == "decision":
        return "boolean"
    if path[0] == "steps" and path[3] == "validated_result":
        current = nodes[path[1]].get("config", {}).get("output_schema", {})
        tail = path[4:]
    elif path[0] in ("input", "inputs"):
        current, tail = schema, path[1:]
    elif path[0] == "work" and path[1] in (
        "mode",
        "scope",
        "current_plan_item_id",
        "feedback",
        "plan_feedback",
    ):
        return "string"
    else:
        return None
    for segment in tail:
        if current.get("properties") and segment not in current["properties"]:
            raise ASTError("Unknown declared field")
        current = current.get("properties", {}).get(segment, {})
    kind = current.get("type")
    return "number" if kind == "integer" else kind if isinstance(kind, str) else None


def _check_ast(
    expression: Any,
    nodes: Mapping[str, Any],
    names: set[str],
    schema: dict[str, Any],
    *,
    condition: bool = False,
) -> ASTNode:
    ast = ASTNode.from_json(expression)
    for path in references(ast):
        validate_reference(path, nodes, names)
        _reference_type(path, nodes, schema)

    def infer(item: ASTNode) -> str | None:
        if item.op == Op.CONST:
            from agents_ide.domain.graph_ast import Value

            return Value.of(item.value).type.value
        if item.op == Op.REF:
            return _reference_type(item.value, nodes, schema)
        children = [infer(c) for c in (item.left, item.right, *item.items) if c is not None]
        concrete = [t for t in children if t not in (None, "null")]
        if item.op in (Op.ALL, Op.ANY, Op.NOT) and any(t != "boolean" for t in concrete):
            raise ASTError("Logical operands must be booleans")
        if item.op in (Op.EQ, Op.NE, Op.GT, Op.GTE, Op.LT, Op.LTE):
            if len(set(concrete)) > 1 or any(t in ("object", "array") for t in concrete):
                raise ASTError("Incompatible types")
            if item.op not in (Op.EQ, Op.NE) and any(t != "number" for t in concrete):
                raise ASTError("Comparison requires numbers")
        return "boolean"

    if condition and infer(ast) not in (None, "boolean", "null"):
        raise ASTError("Condition must be boolean")
    infer(ast)
    if not list(references(ast)) and condition:
        evaluate_truth(ast, EvaluationContext())
    return ast


def _validate_node_content(
    node: dict[str, Any], nodes: dict[str, Any], names: set[str], schema: dict[str, Any]
) -> None:
    config = node.get("config", {})
    _reject_credentials(config)
    choice = config.get("model_selection", {})
    if choice.get("kind") == "direct" and (
        (node["type"] == "AgentTask" and "provider_connection_id" in choice)
        or (node["type"] == "LLMRequest" and "harness_profile_id" in choice)
    ):
        raise ASTError("Direct selection kind mismatch")
    if node["type"] == "Condition":
        _check_ast(node["expression"], nodes, names, schema, condition=True)
    if "output_schema" in config:
        check_data_schema(config["output_schema"])
    for key in ("prompt", "prompt_repair", "prompt_next_item", "message"):
        if key not in config:
            continue
        if len(config[key].encode()) > GRAPH_LIMITS["max_prompt_bytes"]:
            raise ASTError("Prompt exceeds 64 KiB")
        for path in template_references(config[key]):
            validate_reference(path, nodes, names)
            _reference_type(path, nodes, schema)
    if "params" in config:
        validate_parameters(config["params"])
    if "requests_from_node_id" in config and (
        config["requests_from_node_id"] not in nodes
        or nodes[config["requests_from_node_id"]]["type"] not in {"AgentTask", "LLMRequest"}
        or config.get("mode") != "resolve_requests"
    ):
        raise ASTError("Invalid evidence request source")
    if node["type"] in {"GitCommit", "PlanControl"}:
        for key, kinds in (
            ("verification_node_id", {"AgentTask", "LLMRequest"}),
            ("commit_node_id", {"GitCommit"}),
        ):
            if key in config and (
                config[key] not in nodes or nodes[config[key]]["type"] not in kinds
            ):
                raise ASTError("Invalid verification/commit reference")
        if node["type"] == "GitCommit" and isinstance(config.get("allowlist"), dict):
            ast = _check_ast(config["allowlist"], nodes, names, schema)
            if ast.op != Op.REF or ast.value[0] not in ("input", "inputs"):
                raise ASTError("Git allowlist must reference immutable inputs")
        if node["type"] == "PlanControl":
            operation = config["operation"]
            if (
                operation in {"record_verified", "record_final_check"}
                and "verification_node_id" not in config
            ):
                raise ASTError("PlanControl requires an explicit verification reference")
            if (
                operation == "attach_commit"
                or (
                    operation == "record_verified"
                    and config.get("completion_policy", "verified_commit") == "verified_commit"
                )
            ) and "commit_node_id" not in config:
                raise ASTError("PlanControl requires an explicit commit reference")
    if node["type"] == "Command":
        commands = config["commands"]
        selected = config.get("command_filter", [])
        if isinstance(selected, dict):
            ast = _check_ast(selected, nodes, names, schema)
            if (
                ast.op != Op.REF
                or ast.value[0] != "steps"
                or nodes[ast.value[1]]["type"] != "CollectContext"
                or nodes[ast.value[1]].get("config", {}).get("mode") != "resolve_requests"
                or ast.value[3:] != ("validated_result", "command_ids")
            ):
                raise ASTError("Command filter must reference resolved evidence IDs")
            selected = []
        if isinstance(commands, dict):
            ast = _check_ast(commands, nodes, names, schema)
            if ast.op != Op.REF or ast.value[0] not in ("input", "inputs"):
                raise ASTError("Commands must reference immutable inputs")
        else:
            ids = [command["id"] for command in commands]
            if len(set(ids)) != len(ids) or set(selected) - set(ids):
                raise ASTError("Invalid command IDs")
            for command in commands:
                check_relative_path(command.get("cwd", "."))
    if node["type"] == "CollectContext":
        for source in config.get("sources", []):
            required = {
                "file": "path",
                "glob": "path",
                "artifact": "artifact_id",
                "command_report": "command_id",
            }.get(source["kind"])
            if required and not source.get(required):
                raise ASTError("Missing source reference")
            if "path" in source:
                check_relative_path(source["path"])
            if source.get("redact_secrets") is False:
                raise ASTError("Secret redaction cannot be disabled")
        paths = config.get("context_paths", [])
        if isinstance(paths, dict):
            ast = _check_ast(paths, nodes, names, schema)
            if ast.op != Op.REF or ast.value[0] not in ("input", "inputs"):
                raise ASTError("Context paths must reference immutable inputs")
        else:
            for path_text in paths:
                check_relative_path(path_text)
    if node["type"] == "PlanControl":
        required = {
            "record_verified": "verification_node_id",
            "record_final_check": "verification_node_id",
            "attach_commit": "commit_node_id",
        }.get(config["operation"])
        if required and config.get(required) not in nodes:
            raise ASTError("Missing PlanControl node reference")
        if config.get("verification_node_id") and nodes.get(config["verification_node_id"], {}).get(
            "type"
        ) not in ("AgentTask", "LLMRequest"):
            raise ASTError("Expected verifier reference")
        if (
            config.get("commit_node_id")
            and nodes.get(config["commit_node_id"], {}).get("type") != "GitCommit"
        ):
            raise ASTError("Expected GitCommit reference")


def check_relative_path(value: str) -> None:
    from pathlib import PureWindowsPath

    path = PureWindowsPath(value)
    if (
        not value
        or path.drive
        or path.root
        or ".." in path.parts
        or ":" in value
        or "\x00" in value
    ):
        raise ASTError("Path must stay within workspace")
    if any(
        part.lower() in {".git", ".env", "secrets"} or part.lower().startswith(".env.")
        for part in path.parts
    ):
        raise ASTError("Protected path")


PARAM_SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning_effort": {
            "enum": ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
        },
        "temperature": {"type": "number", "minimum": 0, "maximum": 2},
        "top_p": {"type": "number", "minimum": 0, "maximum": 1},
        **{p: {"type": "integer", "minimum": 1} for p in ("max_tokens", "max_output_tokens")},
        "seed": {"type": "integer"},
        "stop": {"type": "array", "items": {"type": "string"}, "maxItems": 16},
        "frequency_penalty": {"type": "number", "minimum": -2, "maximum": 2},
        "presence_penalty": {"type": "number", "minimum": -2, "maximum": 2},
        "stream": {"type": "boolean"},
        "structured_output": {"type": "boolean"},
        "timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 86400},
    },
    "additionalProperties": False,
}


def validate_parameters(params: dict[str, Any]) -> None:
    validate_model_params(params)
    if not Draft202012Validator(PARAM_SCHEMA).is_valid(params):
        raise ASTError("Unsupported model parameter or value")


def _connectivity(
    nodes: dict[str, Any],
    edges: list[dict[str, Any]],
    start: str,
    ends: list[str],
    report: ValidationReport,
) -> None:
    outgoing: dict[str, list[dict[str, Any]]] = defaultdict(list)
    adjacency: dict[str, list[str]] = defaultdict(list)
    reverse: dict[str, list[str]] = defaultdict(list)
    unbounded: dict[str, list[str]] = defaultdict(list)
    loops: dict[str, int] = {}
    for i, edge in enumerate(edges):
        source, target, _ = edge_endpoints(edge)
        assert source is not None and target is not None
        outgoing[source].append(edge)
        adjacency[source].append(target)
        reverse[target].append(source)
        if "loop" in edge:
            loop = edge["loop"]
            if loop["id"] in loops and loops[loop["id"]] != loop["max_iterations"]:
                report.add_error(
                    ValidationIssue(
                        "loop_limit_conflict",
                        "Один цикл должен иметь общий лимит",
                        edge_id=_edge_id(edge, i),
                    )
                )
            loops[loop["id"]] = loop["max_iterations"]
        else:
            unbounded[source].append(target)
    for node_id, node in nodes.items():
        exits = outgoing[node_id]
        valid = len(exits) == (0 if node["type"] == "End" else 1)
        if node["type"] == "Condition":
            valid = len(exits) == 3 and {e.get("when") for e in exits} == {
                "true",
                "false",
                "unknown",
            }
        elif any("when" in e for e in exits):
            valid = False
        if not valid:
            report.add_error(
                ValidationIssue(
                    "transition_ambiguous",
                    "Нужен один переход; Condition требует true/false/unknown, End — ни одного",
                    node_id,
                )
            )
    if reverse[start]:
        report.add_error(
            ValidationIssue("start_has_incoming", "Входящие связи Start запрещены", start)
        )

    def reachable(roots: list[str], links: dict[str, list[str]]) -> set[str]:
        pending, seen = list(roots), set()
        while pending:
            item = pending.pop()
            if item not in seen:
                seen.add(item)
                pending.extend(links[item])
        return seen

    seen = reachable([start], adjacency)
    finishable = reachable(ends, reverse)
    for node_id in nodes:
        if node_id not in seen:
            report.add_error(
                ValidationIssue("node_unreachable", "Узел недостижим из Start", node_id)
            )
        if node_id not in finishable:
            report.add_error(
                ValidationIssue("end_unreachable", "Из узла нет выхода к End", node_id)
            )
    # Removing all explicitly bounded transitions must leave a DAG, regardless of list order.
    indegree = dict.fromkeys(nodes, 0)
    for targets in unbounded.values():
        for target in targets:
            indegree[target] += 1
    pending = [n for n, degree in indegree.items() if not degree]
    while pending:
        for target in unbounded[pending.pop()]:
            indegree[target] -= 1
            if not indegree[target]:
                pending.append(target)
    for i, edge in enumerate(edges):
        source, target, _ = edge_endpoints(edge)
        if "loop" not in edge and indegree.get(source or "", 0) and indegree.get(target or "", 0):
            report.add_error(
                ValidationIssue(
                    "cycle_unbounded",
                    "Цикл требует явного ограниченного обратного перехода",
                    edge_id=_edge_id(edge, i),
                )
            )


def executable_payload(graph: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: deepcopy(value) for key, value in graph.items() if key != "visual"}
    result["nodes"] = sorted(
        [
            {
                key: deepcopy(value)
                for key, value in node.items()
                if key not in {"label", "position", "visual"}
            }
            for node in graph.get("nodes", [])
        ],
        key=lambda n: n["id"],
    )
    for node in result["nodes"]:
        if node["type"] in ("Start", "End") and "config" in node:
            node["config"].pop("label", None)
    edges = []
    for edge in graph.get("edges", []):
        source, target, edge_id = edge_endpoints(edge)
        edges.append(
            {
                "id": edge_id,
                "from": source,
                "to": target,
                **{
                    k: deepcopy(v)
                    for k, v in edge.items()
                    if k
                    not in {
                        "id",
                        "edge_id",
                        "from",
                        "to",
                        "source",
                        "target",
                        "from_node",
                        "to_node",
                        "label",
                        "visual",
                    }
                },
            }
        )
    result["edges"] = sorted(edges, key=lambda e: json.dumps(e, sort_keys=True))
    return result


def import_payload(payload: Any) -> tuple[dict[str, Any], ValidationReport]:
    report = ValidationReport()
    if not isinstance(payload, dict) or not _bounded(payload):
        report.add_error(ValidationIssue("import_invalid", "Импорт должен быть JSON до 1 MiB"))
        return {}, report
    allowed = {
        "schema_version",
        "required_features",
        "graph",
        "inputs",
        "settings",
        "origin",
        "trusted",
        "execution_hash",
    }
    if set(payload) - allowed:
        report.add_error(ValidationIssue("import_field_unknown", "Неизвестное поле импорта"))
    check_version_features(
        payload.get("schema_version", "1.0.0"), payload.get("required_features", []), report
    )
    if not isinstance(payload.get("inputs", {}), dict):
        report.add_error(ValidationIssue("inputs_invalid", "inputs должен быть объектом"))
    if not report.ok:
        return {}, report
    report = validate_graph(payload.get("graph"), inputs=payload.get("inputs", {}))
    if not report.ok:
        return {}, report
    try:
        settings = SettingsOverrides.model_validate(payload.get("settings", {})).model_dump(
            mode="json", exclude_none=True
        )
        _reject_credentials(payload.get("inputs", {}))
        for values in settings.get("role_parameters", {}).values():
            validate_parameters(values)
    except (ValueError, TypeError):
        report.add_error(
            ValidationIssue("import_settings_invalid", "Недопустимые настройки или секреты")
        )
        return {}, report
    features = set(payload.get("required_features", [])) | set(report.features)
    if any(s["kind"] == "group" for s in settings.get("model_selections", {}).values()):
        features.add("model_groups")
    body = {
        "schema_version": "1.0.0",
        "graph": deepcopy(payload["graph"]),
        "inputs": deepcopy(payload.get("inputs", {})),
        "settings": settings,
        "required_features": sorted(features),
        "origin": "imported",
    }
    report.features = sorted(features)
    report.execution_hash = definition_hash(body)
    report.add_warning(
        ValidationIssue(
            "import_trust_required",
            "Перед запуском требуется доверие итоговому execution_hash привязки",
        )
    )
    return body, report


def definition_hash(body: dict[str, Any]) -> str:
    return content_hash(
        {
            "graph": executable_payload(body["graph"]),
            "inputs": body.get("inputs", {}),
            "settings": body.get("settings", {}),
            "schema_version": body.get("schema_version", "1.0.0"),
            "features": sorted(body.get("required_features", [])),
        }
    )


def export_payload(
    graph: Any,
    *,
    inputs: Mapping[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
    schema_version: str = "1.0.0",
    required_features: list[str] | None = None,
) -> dict[str, Any]:
    body, report = import_payload(
        {
            "graph": graph,
            "inputs": dict(inputs or {}),
            "settings": settings or {},
            "schema_version": schema_version,
            "required_features": required_features or [],
        }
    )
    if not report.ok:
        raise AppError(
            "graph_validation_failed", "Нельзя экспортировать неверный граф", 422, report.to_dict()
        )
    return {**body, "execution_hash": report.execution_hash}


def validate_version(session: Session, version_id: str) -> ValidationReport:
    version = session.get(PipelineVersion, version_id)
    if version is None:
        raise AppError("not_found", "Версия не найдена", 404)
    report = validate_graph(json.loads(version.graph_json), inputs=json.loads(version.inputs_json))
    check_version_features(
        version.schema_version, json.loads(version.required_features_json), report
    )
    return report


def validate_with_session(
    session: Session,
    graph: Any,
    binding: PipelineBinding | None = None,
    *,
    inputs: Mapping[str, Any] | None = None,
    settings: SettingsOverrides | None = None,
) -> ValidationReport:
    if binding is not None:
        return preflight(session, binding, inputs=dict(inputs or {}))
    from agents_ide.domain.graph_preflight import _candidates
    from agents_ide.services.settings import DEFAULTS, capture_dependencies

    report = validate_graph(graph, inputs=inputs)
    if not report.ok:
        return report
    configuration = {
        **deepcopy(DEFAULTS),
        **(settings.model_dump(mode="json", exclude_none=True) if settings else {}),
    }
    try:
        dependencies = capture_dependencies(session, graph, configuration)
    except AppError as exc:
        report.add_error(ValidationIssue(exc.code, exc.message, details=exc.details))
        return report
    report.preview = {
        "resolved_settings": configuration,
        "setting_sources": {},
        "candidates": {},
        "data_destinations": [],
    }
    for node in graph["nodes"]:
        config = dependencies["nodes"][node["id"]]
        if node["type"] in ("AgentTask", "LLMRequest") and (
            "model_selection" in config
            or "harness_profile_id" in config
            or "connection_id" in config
        ):
            _candidates(session, node, config, dependencies, configuration, report)
    if dependencies["model_groups"]:
        report.features = sorted({*report.features, "model_groups"})
    return report


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
    from agents_ide.domain.graph_preflight import preflight as run_preflight

    return run_preflight(
        session,
        binding,
        inputs=inputs,
        overrides=overrides,
        workspace_state=workspace_state,
        execution_mode=execution_mode,
        fake_scenario=fake_scenario,
    )
