"""Explicit network-free simulation; scripted writes stay in a private test workspace."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agents_ide.adapters.base import (
    AdapterError,
    AgentAdapter,
    AgentAdapterRequest,
    AgentResult,
    ExternalOutcome,
    LLMAdapter,
    LLMAdapterRequest,
    LLMResult,
)


class FakeResponseSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str = Field(min_length=1, max_length=64)
    visit_index: int | None = Field(default=None, ge=1)
    attempt_index: int = Field(default=1, ge=1)
    raw_text: str = Field(default="{}", max_length=10 * 1024 * 1024)
    decision: Literal["passed", "failed", "inconclusive", "unknown"] | None = None
    outcome: ExternalOutcome = ExternalOutcome.SUCCEEDED
    error_code: str | None = Field(default=None, max_length=64)
    error_message: str | None = Field(default=None, max_length=1024)
    retry_safety: Literal["safe", "unsafe", "unknown"] = "unknown"
    no_effect: bool = False
    delay_seconds: float = Field(default=0, ge=0, le=60, allow_inf_nan=False)
    validated_result: dict[str, Any] | None = None
    files: dict[str, str] = Field(default_factory=dict, max_length=50)
    deltas: list[Annotated[str, Field(max_length=64 * 1024)]] = Field(
        default_factory=list, max_length=200
    )
    tokens_used: int | None = Field(default=None, ge=0)
    cost_estimated: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    budget_quality: Literal["observed", "estimated", "unknown"] = "unknown"

    @model_validator(mode="after")
    def paths_and_size(self) -> FakeResponseSpec:
        for name, value in self.files.items():
            path = PureWindowsPath(name)
            if (
                not name
                or path.drive
                or path.root
                or ".." in path.parts
                or ":" in name
                or "\x00" in name
                or any(p.lower() in {".git", ".env"} for p in path.parts)
            ):
                raise ValueError("Simulated file path must be a safe relative path")
            if len(value.encode("utf-8")) > 256 * 1024:
                raise ValueError("Simulated file exceeds 256 KiB")
        return self


class FakeScenarioSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    responses: list[FakeResponseSpec] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def bounded(self) -> FakeScenarioSpec:
        keys = [(r.node_id, r.visit_index, r.attempt_index) for r in self.responses]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate simulated response key")
        if len(self.model_dump_json().encode("utf-8")) > 1024 * 1024:
            raise ValueError("Scenario exceeds 1 MiB")
        return self


@dataclass(frozen=True)
class FakeResponse:
    node_id: str
    attempt_index: int
    raw_text: str = "{}"
    decision: str | None = None
    outcome: ExternalOutcome = ExternalOutcome.SUCCEEDED
    error_code: str | None = None
    error_message: str | None = None
    delay_seconds: float = 0.0
    validated_result: dict[str, Any] | None = None
    artifacts: tuple[dict[str, Any], ...] = ()
    visit_index: int | None = None
    retry_safety: str = "unknown"
    no_effect: bool = False
    files: dict[str, str] | None = None
    tokens_used: int | None = None
    cost_estimated: float | None = None
    budget_quality: str = "unknown"
    deltas: tuple[str, ...] = ()


@dataclass(frozen=True)
class FakeScenario:
    responses: tuple[FakeResponse, ...] = ()
    simulated: bool = True

    def lookup(self, node_id: str, attempt_index: int, visit_index: int = 1) -> FakeResponse | None:
        for response in self.responses:
            if (
                response.node_id == node_id
                and response.attempt_index == attempt_index
                and response.visit_index in (None, visit_index)
            ):
                return response
        return None


def parse_fake_scenario(payload: Any) -> FakeScenario:
    spec = FakeScenarioSpec.model_validate(payload or {})
    return FakeScenario(tuple(FakeResponse(**r.model_dump()) for r in spec.responses))


def _response(
    request: AgentAdapterRequest | LLMAdapterRequest, scenario: FakeScenario, workspace: Path | None
) -> FakeResponse:
    response = scenario.lookup(
        str(request.context_package.get("__node_id__", "")),
        request.attempt_index,
        request.visit_index,
    )
    if response is None:
        forced = re.search(r"force_decision=(passed|failed|unknown)", request.prompt)
        decision = forced.group(1) if forced else "passed"
        body = {"output": f"simulated {request.model_id}", "verdict": decision}
        response = FakeResponse(
            str(request.context_package.get("__node_id__", "")),
            request.attempt_index,
            raw_text=json.dumps(body),
            validated_result=body,
            decision=decision,
        )
    if request.emit_event:
        for delta in response.deltas:
            request.emit_event("attempt.text_delta", {"text": delta})
    if response.delay_seconds:
        remaining = (
            max(0, request.deadline_at - time.time())
            if request.deadline_at
            else response.delay_seconds
        )
        time.sleep(min(response.delay_seconds, remaining))
    if request.deadline_at is not None and time.time() >= request.deadline_at:
        return FakeResponse(
            response.node_id,
            response.attempt_index,
            outcome=ExternalOutcome.RETRYABLE_FAILURE,
            error_code="timeout",
            retry_safety="safe",
            no_effect=True,
            delay_seconds=response.delay_seconds,
        )
    if response.files:
        if workspace is None:
            raise ValueError("Scripted edits require a private simulation workspace")
        for name, content in response.files.items():
            # Validate again for programmatically constructed fixtures.
            FakeResponseSpec(node_id=response.node_id, files={name: content})
            target = (workspace / Path(*PureWindowsPath(name).parts)).resolve()
            if not target.is_relative_to(workspace.resolve()):
                raise ValueError("Simulated edit escapes its workspace")
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.resolve().is_relative_to(workspace.resolve()):
                raise ValueError("Simulated edit escapes its workspace")
            target.write_text(content, encoding="utf-8")
    return response


def _error(response: FakeResponse) -> AdapterError | None:
    if response.outcome != ExternalOutcome.SUCCEEDED or response.error_code:
        return AdapterError(
            response.error_code or response.outcome.value,
            response.error_message or response.outcome.value,
            response.retry_safety,
        )
    return None


class FakeAgentAdapter(AgentAdapter):
    name = "fake"

    def __init__(self, scenario: FakeScenario, workspace: Path | None = None) -> None:
        self.scenario, self.workspace = scenario, workspace

    def run(self, request: AgentAdapterRequest) -> AgentResult:
        response = _response(request, self.scenario, self.workspace)
        return AgentResult(
            response.outcome,
            response.raw_text,
            response.validated_result,
            response.decision,
            artifacts=response.artifacts,
            error=_error(response),
            elapsed_seconds=response.delay_seconds,
            no_effect=response.no_effect,
        )


class FakeLLMAdapter(LLMAdapter):
    name = "fake"

    def __init__(self, scenario: FakeScenario, workspace: Path | None = None) -> None:
        self.scenario, self.workspace = scenario, workspace

    def run(self, request: LLMAdapterRequest) -> LLMResult:
        response = _response(request, self.scenario, self.workspace)
        return LLMResult(
            response.outcome,
            response.raw_text,
            response.validated_result,
            response.decision,
            error=_error(response),
            elapsed_seconds=response.delay_seconds,
            no_effect=response.no_effect,
            tokens_used=response.tokens_used,
            cost_estimated=response.cost_estimated,
            budget_quality=response.budget_quality,
        )
