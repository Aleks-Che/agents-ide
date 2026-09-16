"""Pydantic contracts for Council (stage 9A) planning endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from agents_ide.domain.contracts import (
    PlanningMemberStatus,
    PlanningRevisionReadiness,
    PlanningState,
)
from agents_ide.domain.schemas import (
    ApiModel,
    ApiOutput,
    DirectAgentSelection,
    DirectLLMSelection,
    GroupSelection,
    ModelSelection,
    ShortStr,
    validate_model_params,
)

# Council session contracts --------------------------------------------------------

PlanningRole = Literal["participant", "merger"]


class PlanningMemberSpec(ApiModel):
    role: PlanningRole = "participant"
    selection: ModelSelection
    params: dict[str, Any] = Field(default_factory=dict, max_length=128)

    @field_validator("params")
    @classmethod
    def _safe_params(cls, value: dict[str, Any]) -> dict[str, Any]:
        validate_model_params(value)
        return value


class PlanningJobBudget(ApiModel):
    max_external_calls: int = Field(default=8, ge=1, le=64)
    max_wallclock_seconds: int = Field(default=600, ge=10, le=3600)
    concurrency: int = Field(default=2, ge=1, le=4)


class PlanningJobCreate(ApiModel):
    project_id: str
    chat_id: str | None = None
    task_text: Annotated[str, StringConstraints(min_length=1, max_length=64 * 1024)]
    context_text: str = Field(default="", max_length=64 * 1024)
    participants: list[PlanningMemberSpec] = Field(default_factory=list, max_length=5)
    budget: PlanningJobBudget = Field(default_factory=PlanningJobBudget)
    idempotency_key: ShortStr
    initiator: ShortStr = "ui"

    @model_validator(mode="after")
    def _participants_xor_budget(self) -> PlanningJobCreate:
        if not self.participants:
            raise ValueError("participants must contain at least one merger entry")
        mergers = [entry for entry in self.participants if entry.role == "merger"]
        if len(mergers) != 1:
            raise ValueError("participants must contain exactly one merger entry")
        participants = [entry for entry in self.participants if entry.role == "participant"]
        n_requested = len(participants)
        if n_requested < 2 or n_requested > 4:
            raise ValueError("Council requires 2..4 participants besides the merger")
        # Each member declares a typed selection (direct agent/llm or group).
        for entry in self.participants:
            if isinstance(entry.selection, DirectAgentSelection) and not entry.selection.model_id:
                raise ValueError("direct agent selection must have a model_id")
            if isinstance(entry.selection, DirectLLMSelection) and not entry.selection.model_id:
                raise ValueError("direct llm selection must have a model_id")
            if isinstance(entry.selection, GroupSelection) and not entry.selection.group_id:
                raise ValueError("group selection must have a group_id")
        return self

    @property
    def participant_count(self) -> int:
        return sum(1 for entry in self.participants if entry.role == "participant")


class PlanningDraftView(ApiOutput):
    body_text: str
    body_truncated: bool
    member_id: str
    slot_index: int
    role: PlanningRole
    selection_kind: Literal["direct", "group"]
    harness_profile_id: str | None
    provider_connection_id: str | None
    model_id: str | None
    status: PlanningMemberStatus
    byte_length: int
    parse_status: Literal["unparsed", "found", "none_found", "invalid_format"]
    accepted: bool
    content_hash: str
    error: str | None = None


class PlanningMemberView(ApiOutput):
    id: str
    slot_index: int
    role: PlanningRole
    selection: dict[str, Any]
    status: PlanningMemberStatus
    selected_member_id: str | None = None
    selected_model_id: str | None = None
    selected_connection_id: str | None = None
    selected_connection_name: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: dict[str, Any] | None = None


class PlanningQuestionOption(ApiOutput):
    id: str
    label: str


class PlanningQuestion(ApiOutput):
    id: ShortStr
    kind: Literal["single", "multi", "text"]
    prompt: str
    options: list[PlanningQuestionOption] = Field(default_factory=list)
    allow_text: bool = True


class PlanningQuestionParse(ApiOutput):
    parse: Literal["found", "none_found", "invalid_format"]
    questions: list[PlanningQuestion] = Field(default_factory=list)


class PlanningRevisionView(ApiOutput):
    plan_items: list[dict[str, Any]] = Field(default_factory=list)
    answers: list[dict[str, Any]] = Field(default_factory=list)
    id: str
    revision_number: int
    author: Literal["merger", "user", "single_member"]
    body_text: str
    body_truncated: bool
    parse: Literal["found", "none_found", "invalid_format"]
    readiness: PlanningRevisionReadiness
    questions: list[PlanningQuestion] = Field(default_factory=list)
    confirmation_hash: str | None = None
    answered_hash: str | None = None
    answered: bool = False
    created_at: datetime
    confirmed_at: datetime | None = None


class PlanningAnswersSubmit(ApiModel):
    expected_revision: int = Field(ge=1)
    answers: list[PlanningAnswerInput] = Field(default_factory=list, max_length=64)
    user_body_text: str | None = Field(default=None, max_length=128 * 1024)


class PlanningAnswerInput(ApiModel):
    question_id: ShortStr
    kind: Literal["single", "multi", "text"]
    selected_option_ids: list[ShortStr] = Field(default_factory=list)
    free_text: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def _kind_constraints(self) -> PlanningAnswerInput:
        if len(set(self.selected_option_ids)) != len(self.selected_option_ids):
            raise ValueError("duplicate option IDs")
        if self.kind == "text" and self.selected_option_ids:
            raise ValueError("text answers cannot select options")
        if self.kind == "single":
            if len(self.selected_option_ids) != 1:
                raise ValueError("single question must have exactly one option selected")
        elif self.kind == "multi":
            if not self.selected_option_ids:
                raise ValueError("multi question must select at least one option")
        elif self.kind == "text" and (not self.free_text or not self.free_text.strip()):
            raise ValueError("text question requires non-empty free_text")
        return self


PlanningAnswersSubmit.model_rebuild()


class PlanningAnswersAccepted(ApiOutput):
    revision_number: int
    questions_hash: str
    readiness: PlanningRevisionReadiness
    requires_merger_revisit: bool
    new_revision: PlanningRevisionView


class PlanningConfirmRequest(ApiModel):
    expected_revision: int = Field(ge=1)
    confirmation_hash: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class PlanningConfirmed(ApiOutput):
    revision_number: int
    confirmation_hash: str
    confirmed_at: datetime


class PlanningCancelRequest(ApiModel):
    expected_state_version: int = Field(ge=0)
    reason: str | None = Field(default=None, max_length=512)


class PlanningRetryRequest(ApiModel):
    expected_state_version: int = Field(ge=0)
    reason: str | None = Field(default=None, max_length=512)
    # Members whose slot indices are listed are reset to pending with
    # candidate_index=0 so the worker can pick them up again. Members not
    # listed are left untouched (typically because they already succeeded
    # and their draft will be merged again).
    reset_member_indices: list[Annotated[int, Field(ge=0, le=4)]] = Field(
        default_factory=list, max_length=5
    )
    reset_all_failed: bool = False
    acknowledge_unknown_result: bool = False
    refresh_credentials: bool = False

    @model_validator(mode="after")
    def _reset_selection(self) -> PlanningRetryRequest:
        if self.reset_all_failed and self.reset_member_indices:
            raise ValueError("Choose reset_all_failed or reset_member_indices")
        if len(set(self.reset_member_indices)) != len(self.reset_member_indices):
            raise ValueError("Duplicate member indices")
        return self


class PlanningRetried(ApiOutput):
    state: PlanningState
    state_version: int
    reset_member_ids: list[str] = Field(default_factory=list)
    preserved_member_ids: list[str] = Field(default_factory=list)


class PlanningPromoteSingleRequest(ApiModel):
    expected_state_version: int = Field(ge=0)
    # The user must explicitly accept the degraded Council. Without this
    # confirmation the API will refuse to create a single-member revision
    # even when the only accepted draft is otherwise valid.
    confirm_degraded: bool = Field(strict=True)

    @model_validator(mode="after")
    def _confirm_required(self) -> PlanningPromoteSingleRequest:
        if not self.confirm_degraded:
            raise ValueError("confirm_degraded must be true to accept a single-member plan")
        return self


class PlanningPromoteSingle(ApiOutput):
    state: PlanningState
    state_version: int
    revision_number: int
    degraded: bool
    n_participants_actual: int
    accepted_member_id: str
    accepted_model_id: str | None


class PlanningProvenance(ApiOutput):
    degraded: bool
    n_participants_requested: int
    n_participants_actual: int
    revision_author: Literal["merger", "user", "single_member"]
    source_member_id: str | None


class PlanningJobView(ApiOutput):
    id: str
    project_id: str
    chat_id: str | None
    idempotency_key: str
    initiator: str
    state: PlanningState
    state_version: int
    task_text: str
    read_manifest_hash: str
    n_participants_requested: int
    n_participants_actual: int
    degraded: bool
    budget: PlanningJobBudget
    usage: dict[str, Any] = Field(default_factory=dict)
    members: list[PlanningMemberView] = Field(default_factory=list)
    drafts: list[PlanningDraftView] = Field(default_factory=list)
    revisions: list[PlanningRevisionView] = Field(default_factory=list)
    events: list[PlanningEventView] = Field(default_factory=list)
    last_error: dict[str, Any] | None = None
    started_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None


class PlanningEventView(ApiOutput):
    sequence: int
    type: str
    member_id: str | None
    payload: dict[str, Any]


PlanningJobView.model_rebuild()
