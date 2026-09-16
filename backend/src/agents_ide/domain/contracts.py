from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class CapabilityStatus(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNVERIFIED = "unverified"


class CapabilityEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: CapabilityStatus
    harness_version: str
    observed_at: datetime
    fixture: str


class AdapterCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    transport: CapabilityStatus = CapabilityStatus.UNVERIFIED
    list_models: CapabilityStatus = CapabilityStatus.UNVERIFIED
    create_session: CapabilityStatus = CapabilityStatus.UNVERIFIED
    model_result: CapabilityStatus = CapabilityStatus.UNVERIFIED
    stream_events: CapabilityStatus = CapabilityStatus.UNVERIFIED
    resume_session: CapabilityStatus = CapabilityStatus.UNVERIFIED
    interrupt: CapabilityStatus = CapabilityStatus.UNVERIFIED
    permission_request: CapabilityStatus = CapabilityStatus.UNVERIFIED
    auth_failure: CapabilityStatus = CapabilityStatus.UNVERIFIED
    transport_recovery: CapabilityStatus = CapabilityStatus.UNVERIFIED
    process_recovery: CapabilityStatus = CapabilityStatus.UNVERIFIED
    write_isolation: CapabilityStatus = CapabilityStatus.UNVERIFIED

    @property
    def transport_ready(self) -> bool:
        return all(
            value == CapabilityStatus.SUPPORTED
            for value in (self.transport, self.create_session, self.model_result)
        )

    def permits_autonomous_write(self, supervisor_verified: bool) -> bool:
        return (
            self.transport_ready
            and supervisor_verified
            and self.write_isolation == CapabilityStatus.SUPPORTED
        )


class RunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED = "paused"
    STOP_REQUESTED = "stop_requested"
    STOPPED = "stopped"
    RETRY_WAIT = "retry_wait"
    WAITING_INPUT = "waiting_input"
    RECOVERING = "recovering"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {self.COMPLETED, self.FAILED, self.CANCELLED}


class PlanningState(StrEnum):
    DRAFTING = "drafting"
    MERGING = "merging"
    NEEDS_ANSWERS = "needs_answers"
    READY_FOR_CONFIRMATION = "ready_for_confirmation"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in {self.CONFIRMED, self.CANCELLED, self.FAILED}


class PlanningMemberStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"

    @property
    def terminal(self) -> bool:
        return self in {
            self.SUCCEEDED,
            self.FAILED,
            self.UNKNOWN,
            self.SKIPPED,
        }


class PlanningRevisionReadiness(StrEnum):
    READY = "ready"
    NEEDS_ANSWERS = "needs_answers"
    UNVERIFIED = "unverified"
    INVALID_FORMAT = "invalid_format"
