"""Pipeline graph schema and JSON Schemas exposed to clients.

This module is the authoritative source of node contracts for stage 3.

* :func:`node_schemas` returns the per-node JSON Schemas (used by clients).
* :func:`graph_limits` exposes the v1 hard limits.
* :func:`required_features_for` derives the required engine features from a
  graph without inspecting the configuration, so that templates and bindings
  can advertise what the engine must support.

The Pydantic models below are kept separate from the public API schemas:
``PipelineDraft`` is intentionally permissive so editors can persist partial
drafts; full validation is performed in :mod:`graph_validation`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

from agents_ide.domain.schemas import MODEL_SELECTION

# --------------------------------------------------------------------------- limits


GRAPH_LIMITS: dict[str, int] = {
    "max_nodes": 200,
    "max_edges": 400,
    "max_prompt_bytes": 65536,
    "max_graph_bytes": 1048576,
    "max_command_subcommands": 50,
    "max_max_output_bytes": 10485760,
    "max_loop_visits_per_run": 1000,
    "max_backward_transitions_per_run": 200,
}


SUPPORTED_SCHEMA_VERSION = "1.0.0"
SUPPORTED_FEATURES = frozenset(
    {
        "agenttask",
        "llmrequest",
        "command",
        "command_filter_ref",
        "llm_http_options",
        "collect_context",
        "git_commit",
        "git",
        "plan_control",
        "verified_plan_git",
        "conditions",
        "model_groups",
        "single_agent",
        "bounded_loops",
        "transition_assignments",
    }
)


# --------------------------------------------------------------------------- node ids

_ID_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_\-]{0,63}$")


def is_valid_node_id(value: str) -> bool:
    return bool(_ID_PATTERN.fullmatch(value))


# --------------------------------------------------------------------------- node schemas

_NODE_TYPES = (
    "Start",
    "End",
    "AgentTask",
    "LLMRequest",
    "Command",
    "CollectContext",
    "GitCommit",
    "Condition",
    "PlanControl",
)


def _node_base() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["id", "type"],
        "properties": {
            "id": {"type": "string", "pattern": _ID_PATTERN.pattern},
            "type": {"enum": list(_NODE_TYPES)},
            "label": {"type": "string", "maxLength": 120},
            "position": {
                "type": "object",
                "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                "additionalProperties": False,
            },
            "visual": {"type": "object"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 86400},
            "max_retries": {"type": "integer", "minimum": 0, "maximum": 5},
        },
        "additionalProperties": False,
    }


def _command_config() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["commands"],
        "properties": {
            "commands": {
                "anyOf": [
                    {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": GRAPH_LIMITS["max_command_subcommands"],
                        "items": {
                            "type": "object",
                            "required": ["id", "program", "args", "success_exit_codes"],
                            "properties": {
                                "id": {"type": "string", "pattern": _ID_PATTERN.pattern},
                                "program": {"type": "string", "minLength": 1, "maxLength": 512},
                                "args": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "maxItems": 64,
                                },
                                "cwd": {"type": "string", "maxLength": 1024},
                                "env": {
                                    "type": "object",
                                    "additionalProperties": {"type": "string"},
                                },
                                "required": {"type": "boolean", "default": True},
                                "success_exit_codes": {
                                    "type": "array",
                                    "items": {"type": "integer"},
                                    "minItems": 1,
                                    "maxItems": 16,
                                },
                                "failure_policy": {"enum": ["collect_all", "stop_on_failure"]},
                                "timeout_seconds": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 86400,
                                },
                                "max_output_bytes": {
                                    "type": "integer",
                                    "minimum": 1024,
                                    "maximum": GRAPH_LIMITS["max_max_output_bytes"],
                                },
                                "retry_safety": {"enum": ["safe", "unsafe", "unknown"]},
                            },
                            "additionalProperties": False,
                        },
                    },
                    {
                        "type": "object",
                        "required": ["ref"],
                        "properties": {"ref": {"type": "string"}},
                        "additionalProperties": False,
                    },
                ]
            },
            "failure_policy": {"enum": ["collect_all", "stop_on_failure"]},
            "result_output": {"type": "string"},
            "command_filter": {
                "anyOf": [
                    {"type": "array", "items": {"type": "string"}, "maxItems": 50},
                    {
                        "type": "object",
                        "required": ["ref"],
                        "properties": {"ref": {"type": "string"}},
                        "additionalProperties": False,
                    },
                ],
            },
        },
        "additionalProperties": False,
    }


def _collect_config() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "mode": {"enum": ["collect", "resolve_requests"]},
            "sources": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["kind"],
                    "properties": {
                        "kind": {"enum": ["file", "glob", "diff", "artifact", "command_report"]},
                        "path": {"type": "string"},
                        "include_untracked": {"type": "boolean"},
                        "max_bytes": {"type": "integer", "minimum": 0},
                        "exclude": {"type": "array", "items": {"type": "string"}},
                        "artifact_id": {"type": "string"},
                        "command_id": {"type": "string"},
                        "redact_secrets": {"type": "boolean"},
                    },
                    "additionalProperties": False,
                },
                "maxItems": 100,
            },
            "context_paths": {
                "oneOf": [
                    {"type": "array", "items": {"type": "string"}, "maxItems": 100},
                    {
                        "type": "object",
                        "required": ["ref"],
                        "properties": {"ref": {"type": "string"}},
                        "additionalProperties": False,
                    },
                ],
            },
            "include_run_history": {"type": "boolean"},
            "include_untracked": {"type": "boolean"},
            "strategy": {"enum": ["summary", "full", "truncate"]},
            "max_files": {"type": "integer", "minimum": 1, "maximum": 100},
            "max_file_bytes": {"type": "integer", "minimum": 1024, "maximum": 262144},
            "max_total_bytes": {"type": "integer", "minimum": 1024, "maximum": 2097152},
            "summary_max_bytes": {"type": "integer", "minimum": 256, "maximum": 65536},
            "max_command_replays": {"type": "integer", "minimum": 0, "maximum": 2},
            "requests_from_node_id": {"type": "string"},
            "missing_evidence_filter": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["kind"],
                    "properties": {
                        "kind": {"enum": ["file", "artifact", "command_report"]},
                        "path": {"type": "string"},
                        "artifact_id": {"type": "string"},
                        "command_id": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
                "maxItems": 50,
            },
        },
        "additionalProperties": False,
    }


def _plan_control_config() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["operation"],
        "properties": {
            "operation": {
                "enum": [
                    "select_next",
                    "record_verified",
                    "record_final_check",
                    "attach_commit",
                ],
            },
            "plan_id": {"type": "string", "pattern": _ID_PATTERN.pattern},
            "verification_node_id": {"type": "string", "pattern": _ID_PATTERN.pattern},
            "commit_node_id": {"type": "string", "pattern": _ID_PATTERN.pattern},
            "scope": {"type": "string", "pattern": _ID_PATTERN.pattern},
            "initial_mode": {"enum": ["initial", "repair", "next_item"]},
            "completion_policy": {"enum": ["verified_commit", "verified_only"]},
        },
        "additionalProperties": False,
    }


def _agent_or_llm_config(node_type: str) -> dict[str, Any]:
    kind = "agent" if node_type == "AgentTask" else "llm"
    base: dict[str, Any] = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "role": {"type": "string", "pattern": _ID_PATTERN.pattern},
            "prompt": {"type": "string", "minLength": 1, "maxLength": 65536},
            "params": {"type": "object", "additionalProperties": True},
            "expected_kind": {"const": kind},
            "max_retries": {"type": "integer", "minimum": 0, "maximum": 5},
            "response_format": {"enum": ["text", "json"]},
            "output_schema": {"type": "object"},
            "prompt_repair": {"type": "string", "minLength": 1, "maxLength": 65536},
            "prompt_next_item": {"type": "string", "minLength": 1, "maxLength": 65536},
            "plan_check": {"enum": ["current", "all"]},
            # Original stage 2 direct channel is explicit and remains supported.
            "connection_id": {"type": "string", "minLength": 1},
            "harness_profile_id": {"type": "string", "minLength": 1},
            "model": {"type": "string", "minLength": 1},
        },
        "additionalProperties": False,
    }
    base["properties"]["model_selection"] = MODEL_SELECTION.json_schema(
        ref_template="#/components/schemas/{model}"
    )
    return base


def _start_or_end_config() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "label": {"type": "string", "maxLength": 120},
        },
        "additionalProperties": False,
    }


def _git_commit_config() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "message": {"type": "string", "maxLength": 512},
            "generate_message": {"type": "boolean"},
            "message_generation": {
                "type": "object",
                "properties": {
                    "connection_id": {"type": "string", "maxLength": 64},
                    "model": {"type": "string", "maxLength": 256},
                    "prompt": {"type": "string", "minLength": 1, "maxLength": 8000},
                    "language": {"enum": ["ru", "en"]},
                    "params": {"type": "object"},
                },
                "additionalProperties": False,
            },
            "allow_untracked": {"type": "boolean"},
            "no_changes_is_progress": {"type": "boolean"},
            "allowlist": {
                "oneOf": [
                    {"type": "array", "minItems": 1, "maxItems": 100, "items": {"type": "string"}},
                    {
                        "type": "object",
                        "required": ["ref"],
                        "properties": {"ref": {"type": "string"}},
                        "additionalProperties": False,
                    },
                ]
            },
            "verification_node_id": {"type": "string", "pattern": _ID_PATTERN.pattern},
            "hook_policy": {"enum": ["allow_pre_configured", "fail_on_unattended"]},
        },
        "additionalProperties": False,
    }


def _condition_config() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


_NODE_SCHEMAS: dict[str, Any] = {
    "Start": {
        **_node_base(),
        "properties": {
            **_node_base()["properties"],
            "type": {"const": "Start"},
            "config": _start_or_end_config(),
        },
        "required": ["id", "type"],
    },
    "End": {
        **_node_base(),
        "properties": {
            **_node_base()["properties"],
            "type": {"const": "End"},
            "config": _start_or_end_config(),
        },
        "required": ["id", "type"],
    },
    "AgentTask": {
        **_node_base(),
        "properties": {
            **_node_base()["properties"],
            "type": {"const": "AgentTask"},
            "config": _agent_or_llm_config("AgentTask"),
        },
        "required": ["id", "type", "config"],
    },
    "LLMRequest": {
        **_node_base(),
        "properties": {
            **_node_base()["properties"],
            "type": {"const": "LLMRequest"},
            "config": _agent_or_llm_config("LLMRequest"),
        },
        "required": ["id", "type", "config"],
    },
    "Command": {
        **_node_base(),
        "properties": {
            **_node_base()["properties"],
            "type": {"const": "Command"},
            "config": _command_config(),
        },
        "required": ["id", "type", "config"],
    },
    "CollectContext": {
        **_node_base(),
        "properties": {
            **_node_base()["properties"],
            "type": {"const": "CollectContext"},
            "config": _collect_config(),
        },
        "required": ["id", "type", "config"],
    },
    "GitCommit": {
        **_node_base(),
        "properties": {
            **_node_base()["properties"],
            "type": {"const": "GitCommit"},
            "config": _git_commit_config(),
        },
        "required": ["id", "type"],
    },
    "Condition": {
        **_node_base(),
        "properties": {
            **_node_base()["properties"],
            "type": {"const": "Condition"},
            "expression": {"type": "object"},
            "config": _condition_config(),
        },
        "required": ["id", "type", "expression"],
    },
    "PlanControl": {
        **_node_base(),
        "properties": {
            **_node_base()["properties"],
            "type": {"const": "PlanControl"},
            "config": _plan_control_config(),
        },
        "required": ["id", "type", "config"],
    },
}


def node_schemas() -> dict[str, Any]:
    """Return the node JSON Schemas with shared ``$defs`` resolved."""

    schemas = deepcopy(_NODE_SCHEMAS)
    choice = MODEL_SELECTION.json_schema(ref_template="#/components/schemas/{model}")
    definitions = choice.pop("$defs")
    for kind in ("AgentTask", "LLMRequest"):
        schemas[kind]["properties"]["config"]["properties"]["model_selection"] = choice
    return {
        "schema_version": SUPPORTED_SCHEMA_VERSION,
        "components": {"schemas": definitions},
        "selection_precedence": ["node", "run", "binding", "template", "default"],
        "parameter_precedence": ["node", "role", "candidate", "profile_or_connection"],
        "nodes": schemas,
        "limits": dict(GRAPH_LIMITS),
        "graph": graph_schema(schemas, definitions),
        "supported_features": sorted(SUPPORTED_FEATURES),
        "ast": {
            "max_depth": 32,
            "operators": [
                "const",
                "ref",
                "eq",
                "ne",
                "gt",
                "gte",
                "lt",
                "lte",
                "exists",
                "all",
                "any",
                "not",
            ],
            "reference_roots": ["input", "inputs", "project", "run", "work", "steps", "artifacts"],
        },
    }


# --------------------------------------------------------------------------- features


def required_features_for(graph: Mapping[str, Any]) -> list[str]:
    """Return engine features required by ``graph`` regardless of configuration."""

    features: set[str] = set()
    for node in graph.get("nodes", []) or []:
        if not isinstance(node, Mapping):
            continue
        node_type = node.get("type")
        config = node.get("config", {})
        if (
            node_type in {"GitCommit", "PlanControl"}
            or config.get("plan_check")
            or config.get("include_run_history")
            or config.get("requests_from_node_id")
            or isinstance(config.get("context_paths"), Mapping)
        ):
            features.add("verified_plan_git")
        if node_type in ("AgentTask", "LLMRequest"):
            features.add(node_type.lower())
        if node_type == "PlanControl":
            features.add("plan_control")
        if node_type == "GitCommit":
            features.add("git_commit")
        if node_type == "CollectContext":
            features.add("collect_context")
        if node_type == "Command":
            features.add("command")
            if isinstance(node.get("config", {}).get("command_filter"), Mapping):
                features.add("command_filter_ref")
        if node_type == "LLMRequest" and any(
            key in node.get("config", {}).get("params", {})
            for key in ("stream", "structured_output", "timeout_seconds")
        ):
            features.add("llm_http_options")
        if node_type == "Condition":
            features.add("conditions")
        if node.get("config", {}).get("model_selection", {}).get("kind") == "group":
            features.add("model_groups")
    for edge in iter_edges(graph):
        if edge.get("loop"):
            features.add("bounded_loops")
            if edge["loop"].get("scope") == "item":
                features.add("verified_plan_git")
        if edge.get("assignments"):
            features.add("transition_assignments")
    return sorted(features)


def graph_schema(nodes: dict[str, Any], definitions: dict[str, Any]) -> dict[str, Any]:
    """A closed executable schema; only visual objects are intentionally extensible."""
    identifier = {"type": "string", "pattern": _ID_PATTERN.pattern}
    edge = {
        "type": "object",
        "properties": {
            "id": identifier,
            "edge_id": identifier,
            **{
                name: identifier
                for name in ("from", "to", "source", "target", "from_node", "to_node")
            },
            "when": {"enum": ["true", "false", "unknown"]},
            "label": {"type": "string"},
            "visual": {"type": "object"},
            "assignments": {
                "type": "object",
                "properties": {
                    name: {"type": "object"}
                    for name in ("work.mode", "work.feedback", "work.plan_feedback")
                },
                "additionalProperties": False,
            },
            "loop": {
                "type": "object",
                "required": ["id", "max_iterations"],
                "properties": {
                    "id": identifier,
                    "max_iterations": {"type": "integer", "minimum": 1, "maximum": 200},
                    "scope": {"enum": ["item", "run"]},
                },
                "additionalProperties": False,
            },
        },
        "additionalProperties": False,
        "allOf": [
            {"oneOf": [{"required": [name]} for name in ("from", "source", "from_node")]},
            {"oneOf": [{"required": [name]} for name in ("to", "target", "to_node")]},
        ],
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "components": {"schemas": definitions},
        "type": "object",
        "required": ["nodes", "edges"],
        "properties": {
            "nodes": {
                "type": "array",
                "minItems": 1,
                "maxItems": 200,
                "items": {"oneOf": list(nodes.values())},
            },
            "edges": {"type": "array", "maxItems": 400, "items": edge},
            "input_schema": {"type": "object"},
            "roles": {"type": "object", "additionalProperties": {"enum": ["agent", "llm"]}},
            "visual": {"type": "object"},
        },
        "additionalProperties": False,
    }


# --------------------------------------------------------------------------- helpers


def graph_size_bytes(graph: Mapping[str, Any]) -> int:
    return len(json.dumps(graph, ensure_ascii=False).encode("utf-8"))


def iter_nodes(graph: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    nodes = graph.get("nodes") or []
    for node in nodes:
        if isinstance(node, Mapping):
            yield node


def iter_edges(graph: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    edges = graph.get("edges") or []
    for edge in edges:
        if isinstance(edge, Mapping):
            yield edge


def edge_endpoints(edge: Mapping[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Return (from_id, to_id, edge_id) tuple from a loose edge dict."""

    source = edge.get("from") or edge.get("source") or edge.get("from_node")
    target = edge.get("to") or edge.get("target") or edge.get("to_node")
    edge_id = edge.get("id") or edge.get("edge_id")
    return (
        source if isinstance(source, str) else None,
        target if isinstance(target, str) else None,
        edge_id if isinstance(edge_id, str) else None,
    )
