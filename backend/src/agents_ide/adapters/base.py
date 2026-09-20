"""Adapter result envelopes and shared error taxonomy.

External adapters do not raise structured exceptions; they translate their
native errors into :class:`ExternalOutcome` codes which the runner maps to
domain policies (confirmed failure vs. retryable error vs. unavailable model).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from threading import Event
from typing import Any


class ExternalOutcome(StrEnum):
    """Single-word verdict the adapter returns about the external call."""

    SUCCEEDED = "succeeded"
    RETRYABLE_FAILURE = "retryable_failure"
    CONFIRMED_FAILURE = "confirmed_failure"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"
    PERMISSION_DENIED = "permission_denied"
    INVALID_FORMAT = "invalid_format"
    TRANSPORT_DROPPED = "transport_dropped"
    PROCESS_DIED = "process_died"


@dataclass(frozen=True)
class AdapterError:
    code: str
    message: str
    retry_safety: str = "unknown"
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentResult:
    """Result returned by an agent adapter for one step attempt."""

    outcome: ExternalOutcome
    raw_text: str
    validated_result: dict[str, Any] | None
    decision: str | None
    artifacts: tuple[dict[str, Any], ...] = ()
    tool_calls: tuple[dict[str, Any], ...] = ()
    error: AdapterError | None = None
    elapsed_seconds: float = 0.0
    finished_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    no_effect: bool = False
    tokens_used: int | None = None
    cost_estimated: float | None = None
    budget_quality: str | None = None
    # A provider/transport failure allows another agent to continue existing
    # work once the runner confirms process termination. This does not mean the
    # entire attempt had no effects or can be replayed.
    can_handoff: bool = False

    @property
    def succeeded(self) -> bool:
        return self.outcome == ExternalOutcome.SUCCEEDED


@dataclass(frozen=True)
class LLMResult:
    """Result returned by an LLM adapter for one step attempt."""

    outcome: ExternalOutcome
    raw_text: str
    validated_result: dict[str, Any] | None
    decision: str | None
    error: AdapterError | None = None
    tokens_used: int | None = None
    cost_estimated: float | None = None
    budget_quality: str | None = None
    elapsed_seconds: float = 0.0
    finished_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    no_effect: bool = False
    result_schema: str | None = None
    # A finished text-only provider request can move to another candidate even
    # when its token usage is unknown. This is not permission to replay tools.
    can_fallback: bool = False

    @property
    def succeeded(self) -> bool:
        return self.outcome == ExternalOutcome.SUCCEEDED


@dataclass(frozen=True)
class AgentAdapterRequest:
    role: str
    model_id: str
    prompt: str
    context_package: dict[str, Any]
    workspace_path: str
    capabilities: dict[str, Any]
    params: dict[str, Any]
    resume_session_id: str | None = None
    resume_required: bool = False
    feedback: str | None = None
    attempt_index: int = 0
    visit_index: int = 1
    deadline_at: float | None = None
    emit_event: Callable[[str, dict[str, Any]], None] | None = None
    stop_event: Event | None = None
    check_owned: Callable[[], None] | None = None
    receive_message: Callable[[], dict[str, Any] | None] | None = None
    # Standalone protocol diagnostics retain envelopes; the IDE disables this archive.
    record_tool_history: bool = True


@dataclass(frozen=True)
class LLMAdapterRequest:
    role: str
    model_id: str
    prompt: str
    context_package: dict[str, Any]
    params: dict[str, Any]
    response_format: str = "text"
    output_schema: dict[str, Any] | None = None
    attempt_index: int = 0
    visit_index: int = 1
    deadline_at: float | None = None
    emit_event: Callable[[str, dict[str, Any]], None] | None = None
    stop_event: Event | None = None
    check_owned: Callable[[], None] | None = None
    connection: dict[str, Any] | None = None


class AgentAdapter:
    """Adapter contract for agent harnesses (Codex, OpenCode, fake).

    Implementations are intentionally synchronous so the engine runner can
    dispatch them without an event loop. Real adapters at stage 6 will
    delegate to their own thread or subprocess internally.
    """

    name: str = "agent"

    def run(self, request: AgentAdapterRequest) -> AgentResult:
        raise NotImplementedError

    def interrupt(self, session_id: str) -> None:
        return None


class LLMAdapter:
    """Adapter contract for OpenAI-compatible / loopback LLM endpoints."""

    name: str = "llm"

    def run(self, request: LLMAdapterRequest) -> LLMResult:
        raise NotImplementedError
