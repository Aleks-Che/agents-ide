"""Pipeline graph AST: three-valued conditions and context references.

The AST is intentionally small: only `const`, `ref`, comparison, logical
operators and existence checks. Values are strictly typed boolean / number /
string / null with no implicit conversions. Missing values yield ``unknown``;
references to undefined nodes, scopes or fields are configuration errors.

Execution happens in two stages:

1. :func:`evaluate` produces a :class:`TruthValue` (``true`` / ``false`` /
   ``unknown``) for conditions and a :class:`Value` for expressions used in
   assignments or context packages.
2. :func:`substitute` performs a single-pass textual interpolation of
   ``{{ path }}`` references inside prompts and messages, replacing them with
   the rendered ``Value`` or raising a configuration error when the reference
   points outside the known scope.

The full evaluation context (``scope``) is supplied by the caller: in v1 it is
built from the immutable inputs, the active ``PlanControl.work`` projection
and the ``latest`` results of completed nodes.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ASTError(ValueError):
    """Raised when an AST node references an undefined node, scope or field."""


class TruthValue(StrEnum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


class ValueType(StrEnum):
    BOOLEAN = "boolean"
    NUMBER = "number"
    STRING = "string"
    NULL = "null"
    MISSING = "missing"
    OBJECT = "object"
    ARRAY = "array"


@dataclass(frozen=True)
class Value:
    type: ValueType
    raw: Any = None

    @classmethod
    def of(cls, raw: Any) -> Value:
        if raw is None:
            return cls(ValueType.NULL)
        if isinstance(raw, bool):
            return cls(ValueType.BOOLEAN, raw)
        if isinstance(raw, (int, float)):
            if not math.isfinite(raw):
                raise ASTError("Number must be finite")
            return cls(ValueType.NUMBER, raw)
        if isinstance(raw, str):
            return cls(ValueType.STRING, raw)
        if isinstance(raw, (dict, list)):
            json.dumps(raw, allow_nan=False)
            return cls(ValueType.OBJECT if isinstance(raw, dict) else ValueType.ARRAY, raw)
        raise ASTError(f"Unsupported runtime value type: {type(raw).__name__}")

    @classmethod
    def missing(cls) -> Value:
        return cls(ValueType.MISSING)

    @property
    def missing_or_null(self) -> bool:
        return self.type in (ValueType.MISSING, ValueType.NULL)

    def truth(self) -> TruthValue:
        if self.type == ValueType.MISSING:
            return TruthValue.UNKNOWN
        if self.type == ValueType.NULL:
            return TruthValue.UNKNOWN
        if self.type == ValueType.BOOLEAN:
            return TruthValue.TRUE if self.raw else TruthValue.FALSE
        raise ASTError("Condition requires boolean, null or missing")

    def render(self) -> str:
        if self.type == ValueType.MISSING:
            return ""
        if self.type == ValueType.NULL:
            return ""
        if self.type == ValueType.STRING:
            return str(self.raw)
        if self.type == ValueType.BOOLEAN:
            return "true" if self.raw else "false"
        if self.type in (ValueType.OBJECT, ValueType.ARRAY):
            return json.dumps(self.raw, ensure_ascii=False)
        return str(self.raw)

    def compatible(self, other: Value) -> bool:
        if self.type == ValueType.MISSING or other.type == ValueType.MISSING:
            return False
        return self.type == other.type


class Op(StrEnum):
    CONST = "const"
    REF = "ref"
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    EXISTS = "exists"
    ALL = "all"
    ANY = "any"
    NOT = "not"


@dataclass(frozen=True)
class ASTNode:
    op: Op
    value: Any = None
    left: ASTNode | None = None
    right: ASTNode | None = None
    items: tuple[ASTNode, ...] = field(default_factory=tuple)

    @classmethod
    def from_json(cls, payload: Any, *, _depth: int = 0) -> ASTNode:
        if _depth > 32:
            raise ASTError("AST exceeds depth 32")
        if not isinstance(payload, dict):
            raise ASTError("AST node must be an object")
        if "op" in payload:
            try:
                op = Op(payload["op"])
            except (ValueError, TypeError) as exc:
                raise ASTError(f"Unknown AST op: {payload['op']}") from exc
        elif "const" in payload:
            if set(payload) != {"const"}:
                raise ASTError("Unknown const fields")
            # ``{'const': value}`` shorthand for ``{'op': 'const', 'value': value}``.
            raw = payload["const"]
            if isinstance(raw, (bool, int, float, str)) or raw is None:
                Value.of(raw)
                return cls(op=Op.CONST, value=raw)
            raise ASTError("'const' accepts only boolean/number/string/null")
        elif "ref" in payload or "path" in payload:
            if len(payload) != 1:
                raise ASTError("Unknown ref fields")
            path = payload.get("path") or payload.get("ref")
            if path is None:
                raise ASTError("'ref' payload requires 'path' or 'ref'")
            return cls(op=Op.REF, value=cls._parse_ref(path))
        else:
            raise ASTError("AST node requires 'op'")
        fields = {
            Op.CONST: {"value"},
            Op.REF: {"path"},
            Op.EXISTS: {"target"},
            Op.NOT: {"operand"},
            Op.ALL: {"items"},
            Op.ANY: {"items"},
        }.get(op, {"left", "right"})
        if set(payload) != {"op", *fields}:
            raise ASTError(f"Invalid fields for '{op.value}'")
        if op in (Op.EQ, Op.NE, Op.GT, Op.GTE, Op.LT, Op.LTE):
            if "left" not in payload or "right" not in payload:
                raise ASTError(f"'{op.value}' requires both 'left' and 'right'")
            return cls(
                op=op,
                left=cls.from_json(payload["left"], _depth=_depth + 1),
                right=cls.from_json(payload["right"], _depth=_depth + 1),
            )
        if op in (Op.EXISTS,):
            target = payload.get("target")
            if target is None:
                raise ASTError("'exists' requires 'target'")
            return cls(op=op, value=cls._parse_ref(target))
        if op in (Op.NOT,):
            return cls(op=op, left=cls.from_json(payload["operand"], _depth=_depth + 1))
        if op in (Op.ALL, Op.ANY):
            raw = payload.get("items")
            if not isinstance(raw, list) or not 1 <= len(raw) <= 200:
                raise ASTError(f"'{op.value}' requires a non-empty 'items' list")
            return cls(op=op, items=tuple(cls.from_json(item, _depth=_depth + 1) for item in raw))
        if op == Op.CONST:
            return cls._const_from_json(payload)
        if op == Op.REF:
            if "path" not in payload:
                raise ASTError("'ref' requires 'path'")
            return cls(op=op, value=cls._parse_ref(payload["path"]))
        raise ASTError(f"Unsupported AST op: {op}")

    @classmethod
    def _const_from_json(cls, payload: Mapping[str, Any]) -> ASTNode:
        if "value" not in payload:
            raise ASTError("'const' requires 'value'")
        raw = payload["value"]
        if isinstance(raw, (bool, int, float, str)) or raw is None:
            Value.of(raw)
            return cls(op=Op.CONST, value=raw)
        raise ASTError("'const' accepts only boolean/number/string/null")

    @staticmethod
    def _parse_ref(payload: Any) -> tuple[str, ...]:
        if isinstance(payload, str):
            return _split_path(payload)
        if isinstance(payload, list) and payload and all(isinstance(item, str) for item in payload):
            return _split_path(".".join(payload))
        raise ASTError("Reference path must be a dotted string or a list of segments")


def _split_path(text: str) -> tuple[str, ...]:
    parts = tuple(text.split("."))
    if (
        len(parts) < 2
        or len(parts) > 16
        or any(not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", segment) for segment in parts)
    ):
        raise ASTError("Invalid reference path")
    return parts


@dataclass(frozen=True)
class EvaluationContext:
    """Runtime values accessible from AST references.

    ``inputs`` maps declared input names to values. ``latest`` maps node IDs to
    their :class:`LatestResult`. ``work`` exposes the current PlanControl
    projection (``current_plan_item_id``, ``scope``, ``plan_item_ids`` ...).
    """

    inputs: Mapping[str, Value] = field(default_factory=dict)
    latest: Mapping[str, LatestResult] = field(default_factory=dict)
    work: Mapping[str, Value] = field(default_factory=dict)
    known_node_ids: frozenset[str] = field(default_factory=frozenset)
    required_inputs: frozenset[str] = field(default_factory=frozenset)
    project: Mapping[str, Value] = field(default_factory=dict)
    run: Mapping[str, Value] = field(default_factory=dict)
    artifacts: Mapping[str, Value] = field(default_factory=dict)
    cycle_id: int | None = None
    scope: str | None = None
    evidence_manifest_id: str | None = None


@dataclass(frozen=True)
class LatestResult:
    decision: TruthValue
    validated_result: Value | None = None
    evidence_manifest_id: str | None = None
    plan_item_ids: tuple[str, ...] = ()
    execution_id: str | None = None
    attempt_id: str | None = None
    cycle_id: int | None = None
    scope: str | None = None
    raw_result_ref: str | None = None


def evaluate(node: ASTNode, context: EvaluationContext) -> Value:
    """Evaluate ``node`` and return its raw :class:`Value`.

    Raises :class:`ASTError` when a reference resolves outside the context.
    """

    if node.op == Op.CONST:
        return Value.of(node.value) if node.value is not None else Value(ValueType.NULL)
    if node.op == Op.REF:
        return _resolve_ref(node.value, context)
    if node.op == Op.EXISTS:
        return Value.of(_resolve_ref(node.value, context).type != ValueType.MISSING)
    if node.op == Op.NOT:
        if node.left is None:
            return Value.of(None)
        inner = evaluate(node.left, context).truth()
        if inner == TruthValue.UNKNOWN:
            return Value.of(None)
        return Value.of(inner != TruthValue.TRUE)
    if node.op in (Op.ALL, Op.ANY):
        items = [evaluate(child, context) for child in node.items]
        return _aggregate_truth(node.op, items)
    if node.op in (Op.EQ, Op.NE, Op.GT, Op.GTE, Op.LT, Op.LTE):
        left = evaluate(node.left, context) if node.left is not None else Value.missing()
        right = evaluate(node.right, context) if node.right is not None else Value.missing()
        return _compare(node.op, left, right)
    raise ASTError(f"Unsupported AST op: {node.op}")


def evaluate_truth(node: ASTNode, context: EvaluationContext) -> TruthValue:
    value = evaluate(node, context)
    return value.truth()


def _resolve_ref(path: tuple[str, ...], context: EvaluationContext) -> Value:
    if not path:
        raise ASTError("Empty reference path")
    head, *rest = path
    tail = tuple(rest)
    if head in ("inputs", "input"):
        return _walk_mapping(context.inputs, tail, "inputs")
    if head == "work":
        validate_reference(path, context.known_node_ids)
        return _walk_mapping(context.work, tail, "work")
    if head == "steps":
        return _walk_steps(tail, context)
    if head in ("project", "run", "artifacts"):
        validate_reference(path, context.known_node_ids)
        return _walk_mapping(getattr(context, head), tail, head)
    raise ASTError(f"Unknown reference root: {head}")


def _walk_mapping(values: Mapping[str, Value], path: Iterable[str], label: str) -> Value:
    current: Any = values
    segments = tuple(path)
    for segment in segments:
        if isinstance(current, Value):
            if current.type in (ValueType.MISSING, ValueType.NULL):
                return Value.missing()
            current = current.raw
        if isinstance(current, Mapping) and segment in current:
            current = current[segment]
            continue
        if not segments:
            return Value.missing()
        if isinstance(current, Mapping) and segment not in current:
            return Value.missing()
        raise ASTError(f"Unknown {label} field: {'.'.join((label,) + segments)}")
    if isinstance(current, Value):
        return current
    return Value.of(current)


def _walk_steps(path: tuple[str, ...], context: EvaluationContext) -> Value:
    validate_reference(("steps", *path), context.known_node_ids)
    if not path:
        raise ASTError("Empty steps path")
    node_id, *rest = path
    if node_id not in context.known_node_ids:
        raise ASTError(f"Unknown node reference: {node_id}")
    latest = context.latest.get(node_id)
    if (
        latest is None
        or (context.cycle_id is not None and latest.cycle_id != context.cycle_id)
        or (context.scope is not None and latest.scope != context.scope)
        or (
            context.evidence_manifest_id is not None
            and latest.evidence_manifest_id != context.evidence_manifest_id
        )
    ):
        return Value.missing()
    if not rest:
        return Value.of(None)
    head, *tail = rest
    if head == "latest":
        if not tail:
            return Value.of(None)
        field_name, *extra = tail
        value = _latest_field(latest, field_name)
        if extra:
            if field_name == "validated_result":
                return _walk_mapping({"result": value}, ("result", *extra), "validated_result")
            raise ASTError(
                f"Unknown steps field: {'.'.join(('steps', node_id, 'latest', field_name))}"
            )
        return value
    if head == "decision":
        return Value.of(latest.decision.value)
    raise ASTError(f"Unknown steps field: {'.'.join(('steps', node_id, head))}")


def _latest_field(latest: LatestResult, field: str) -> Value:
    if field == "raw_result_ref":
        return Value.of(latest.raw_result_ref)
    if field == "decision":
        if latest.decision == TruthValue.TRUE:
            return Value.of(True)
        if latest.decision == TruthValue.FALSE:
            return Value.of(False)
        return Value(ValueType.NULL)
    if field == "validated_result":
        return latest.validated_result or Value.missing()
    if field == "evidence_manifest_id":
        return Value.of(latest.evidence_manifest_id)
    if field == "plan_item_ids":
        return Value.of(list(latest.plan_item_ids))
    if field == "execution_id":
        return Value.of(latest.execution_id)
    if field == "attempt_id":
        return Value.of(latest.attempt_id)
    if field == "cycle_id":
        return Value.of(latest.cycle_id)
    raise ASTError(f"Unknown latest field: {field}")


def _aggregate_truth(op: Op, items: Iterable[Value]) -> Value:
    truth = [item.truth() for item in items]
    if op == Op.ALL:
        if any(t == TruthValue.FALSE for t in truth):
            return Value.of(False)
        if any(t == TruthValue.UNKNOWN for t in truth):
            return Value.of(None)
        return Value.of(True)
    if any(t == TruthValue.TRUE for t in truth):
        return Value.of(True)
    if any(t == TruthValue.UNKNOWN for t in truth):
        return Value.of(None)
    return Value.of(False)


def _compare(op: Op, left: Value, right: Value) -> Value:
    if left.type == ValueType.MISSING or right.type == ValueType.MISSING:
        return Value.of(None)
    if left.type == ValueType.NULL or right.type == ValueType.NULL:
        return Value.of(None)
    if left.type in (ValueType.OBJECT, ValueType.ARRAY) or right.type in (
        ValueType.OBJECT,
        ValueType.ARRAY,
    ):
        raise ASTError("Comparisons require scalar values")
    if op == Op.EQ:
        if left.type != right.type:
            raise ASTError("Incompatible comparison types")
        return Value.of(left.raw == right.raw)
    if op == Op.NE:
        if left.type != right.type:
            raise ASTError("Incompatible comparison types")
        return Value.of(left.raw != right.raw)
    if not (left.type == right.type == ValueType.NUMBER):
        raise ASTError("Ordered comparison requires numbers")
    if op == Op.GT:
        return Value.of(left.raw > right.raw)
    if op == Op.GTE:
        return Value.of(left.raw >= right.raw)
    if op == Op.LT:
        return Value.of(left.raw < right.raw)
    if op == Op.LTE:
        return Value.of(left.raw <= right.raw)
    return Value.of(None)


# --------------------------------------------------------------------------- substitution


_SUBSTITUTION_PATTERN = re.compile(r"\{\{\s*([^}{]+?)\s*\}\}")


def substitute(text: str, context: EvaluationContext, *, strict: bool = True) -> str:
    """Replace ``{{ path }}`` placeholders in ``text``.

    When ``strict`` is true a missing reference raises :class:`ASTError`.
    Otherwise the placeholder is left intact so prompts can be inspected
    during preflight. Non-string input passes through unchanged.
    """

    if not isinstance(text, str):
        return text

    def replace(match: re.Match[str]) -> str:
        path = match.group(1)
        try:
            node = ASTNode.from_json({"op": "ref", "path": path})
            value = evaluate(node, context)
        except ASTError:
            if strict:
                raise
            return match.group(0)
        if value.type == ValueType.MISSING:
            if strict:
                raise ASTError(f"Missing required value: {path}")
            return match.group(0)
        return value.render()

    return _SUBSTITUTION_PATTERN.sub(replace, text)


def validate_required_inputs(
    inputs: Mapping[str, Value], declared: Iterable[str]
) -> tuple[str, ...]:
    missing = tuple(sorted(name for name in declared if name not in inputs))
    return missing


def known_roots() -> tuple[str, ...]:
    return ("input", "inputs", "project", "run", "work", "steps", "artifacts")


WORK_FIELDS = {
    "current_plan_item_id",
    "scope",
    "plan_item_ids",
    "completed_items",
    "remaining_items",
    "mode",
    "feedback",
    "plan_feedback",
    "cycle_id",
}
LATEST_FIELDS = {
    "raw_result_ref",
    "validated_result",
    "decision",
    "evidence_manifest_id",
    "plan_item_ids",
    "execution_id",
    "attempt_id",
    "cycle_id",
}


def validate_reference(
    path: tuple[str, ...],
    known_nodes: Iterable[str],
    input_names: Iterable[str] | None = None,
) -> None:
    root, *tail = path
    if root not in known_roots() or not tail:
        raise ASTError("Unknown reference scope")
    fields = {
        "work": WORK_FIELDS,
        "project": {"id", "name", "path", "settings"},
        "run": {"id", "cycle_id", "remaining_limits"},
        "artifacts": {"context", "reports", "diff", "files", "commands"},
    }
    if root in fields and tail[0] not in fields[root]:
        raise ASTError(f"Unknown {root} field: {tail[0]}")
    if root in ("inputs", "input") and input_names is not None and tail[0] not in input_names:
        raise ASTError(f"Unknown input: {tail[0]}")
    if root == "steps":
        if tail[0] not in known_nodes:
            raise ASTError(f"Unknown node reference: {tail[0]}")
        if len(tail) < 3 or tail[1] != "latest" or tail[2] not in LATEST_FIELDS:
            raise ASTError("Unknown steps field")
        if len(tail) > 3 and tail[2] != "validated_result":
            raise ASTError("Scalar latest field cannot be traversed")


def references(node: ASTNode) -> Iterable[tuple[str, ...]]:
    if node.op in (Op.REF, Op.EXISTS):
        yield node.value
    for child in (node.left, node.right, *node.items):
        if child is not None:
            yield from references(child)


def template_references(text: str) -> Iterable[tuple[str, ...]]:
    for match in _SUBSTITUTION_PATTERN.finditer(text):
        yield _split_path(match.group(1))
    remainder = _SUBSTITUTION_PATTERN.sub("", text)
    if "{{" in remainder or "}}" in remainder:
        raise ASTError("Malformed substitution")


def apply_assignments(
    assignments: Mapping[str, Any], context: EvaluationContext
) -> dict[str, Value]:
    """Evaluate all RHS against the old context, then atomically return new work values."""
    updates = {}
    for target, expression in assignments.items():
        if target not in {"work.mode", "work.feedback", "work.plan_feedback"}:
            raise ASTError("Transition cannot change immutable inputs or PlanControl fields")
        value = evaluate(ASTNode.from_json(expression), context)
        if value.type == ValueType.MISSING:
            raise ASTError("Missing assignment value")
        if target == "work.mode" and value.raw not in ("initial", "repair", "next_item"):
            raise ASTError("Invalid work.mode")
        updates[target[5:]] = value
    return {**context.work, **updates}
