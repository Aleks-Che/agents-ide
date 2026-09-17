"""Durable Council preparation and immutable, explicitly confirmed revisions."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agents_ide.domain.common import content_hash, new_id, utc_now
from agents_ide.domain.contracts import PlanningRevisionReadiness, PlanningState
from agents_ide.domain.graph_validation import validate_parameters
from agents_ide.domain.planning import (
    PlanningAnswersAccepted,
    PlanningAnswersSubmit,
    PlanningCancelRequest,
    PlanningConfirmed,
    PlanningConfirmRequest,
    PlanningDraftView,
    PlanningEventView,
    PlanningJobCreate,
    PlanningJobView,
    PlanningMemberView,
    PlanningPromoteSingle,
    PlanningPromoteSingleRequest,
    PlanningRetried,
    PlanningRetryRequest,
    PlanningRevisionView,
)
from agents_ide.domain.planning_document import PlanDocument, parse_document
from agents_ide.domain.schemas import PlanningSource
from agents_ide.engine.artifacts import sanitize
from agents_ide.errors import AppError
from agents_ide.persistence.models import (
    Chat,
    HarnessProfile,
    PlanningAnswer,
    PlanningAttempt,
    PlanningDraft,
    PlanningEvent,
    PlanningJob,
    PlanningMember,
    PlanningRevision,
    Project,
    ProviderConnection,
)
from agents_ide.security.native_credentials import fingerprint
from agents_ide.services.groups import load_group_snapshot
from agents_ide.services.transactions import begin_write


def _serialise(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _dt(value: float | None) -> datetime | None:
    return datetime.fromtimestamp(value, UTC) if value else None


def _record_event(
    session: Session,
    job_id: str,
    *,
    event_type: str,
    member_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    # Every caller holds BEGIN IMMEDIATE. Flush pending events rather than caching
    # sequence numbers across commits/rollbacks in a long-lived session.
    session.flush()
    sequence = (
        session.scalar(
            select(func.max(PlanningEvent.sequence)).where(PlanningEvent.job_id == job_id)
        )
        or 0
    )
    session.add(
        PlanningEvent(
            id=new_id(),
            job_id=job_id,
            member_id=member_id,
            sequence=sequence + 1,
            event_version=1,
            type=event_type,
            occurred_at=utc_now(),
            payload_json=_serialise(sanitize(payload or {})),
        )
    )


def get_planning_job(session: Session, job_id: str) -> PlanningJob:
    job = session.get(PlanningJob, job_id)
    if job is None:
        raise AppError("planning_not_found", "Planning job not found", 404)
    return job


def latest_revision(session: Session, job_id: str) -> PlanningRevision | None:
    return session.scalar(
        select(PlanningRevision)
        .where(PlanningRevision.job_id == job_id)
        .order_by(PlanningRevision.revision_number.desc())
        .limit(1)
    )


def _next_revision_number(session: Session, job_id: str) -> int:
    session.flush()
    previous = latest_revision(session, job_id)
    return previous.revision_number + 1 if previous else 1


def _candidate(
    session: Session,
    selection: dict[str, Any],
    params: dict[str, Any],
    member_id: str | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    """Build an immutable candidate snapshot for one slot.

    Both LLM (``provider_connection_id``) and harness (``harness_profile_id``)
    selections share the same shape so the worker can iterate over a single
    list. The kind is derived from the persisted fields.
    """
    validate_parameters(params)
    profile_id = selection.get("harness_profile_id")
    if profile_id:
        profile = session.get(HarnessProfile, profile_id)
        if profile is None or profile.archived_at is not None:
            raise AppError("harness_unavailable", "Council harness profile unavailable", 409)
        # Until stage 6 opens the real-model gate, only the same read-only/never
        # permissions are accepted. Any unverified policy change fails fast.
        if not _harness_is_read_only(profile):
            raise AppError(
                "council_harness_unverified",
                "Council harness must declare an explicit read-only/no-tools policy",
                422,
                {"harness_profile_id": profile.id, "harness_kind": profile.harness_kind},
            )
        return {
            "member_id": member_id,
            "enabled": enabled,
            "kind": "agent",
            "model_id": selection["model_id"],
            "params": params,
            "harness_profile_id": profile.id,
            "harness": {
                "id": profile.id,
                "name": profile.name,
                "harness_kind": profile.harness_kind,
                "executable_path": profile.executable_path,
                "settings": json.loads(profile.settings_json or "{}"),
                "version": profile.version,
                "native_fingerprint": fingerprint(profile.harness_kind, profile.executable_path),
            },
        }
    connection = session.get(ProviderConnection, selection.get("provider_connection_id"))
    if connection is None or connection.archived_at is not None:
        raise AppError("connection_unavailable", "Council connection unavailable", 409)
    return {
        "member_id": member_id,
        "enabled": enabled,
        "kind": "llm",
        "model_id": selection["model_id"],
        "params": params,
        "provider_connection_id": connection.id,
        "connection": {
            "id": connection.id,
            "name": connection.name,
            "base_url": connection.base_url,
            "version": connection.version,
            "secret_reference": connection.secret_reference,
        },
    }


def _harness_is_read_only(profile: HarnessProfile) -> bool:
    """Harness must enforce read-only/no-tools for Council preparation.

    The settings shape follows what ``engine.opencode_runtime.validate_settings``
    and ``engine.codex_runtime.validate_settings`` already accept. Other shapes
    remain unverified and are refused to avoid silent capability drift.
    """
    try:
        settings = json.loads(profile.settings_json or "{}")
    except ValueError:
        return False
    if not isinstance(settings, dict):
        return False
    from agents_ide.engine.codex_runtime import validate_settings as validate_codex
    from agents_ide.engine.opencode_runtime import validate_settings as validate_opencode

    try:
        if profile.harness_kind == "opencode":
            validate_opencode(settings)
        elif profile.harness_kind == "codex":
            validate_codex(settings)
        else:
            return False
    except AppError:
        return False
    return True


def candidate_identity(candidate: dict[str, Any]) -> tuple[str, str]:
    """Independence key for a candidate.

    LLM candidates are keyed by the normalised endpoint and model id. Harness
    candidates use the model ID across harness kinds: changing the transport
    does not create an independent vote. Unknown aliases remain unverified.
    """
    if candidate.get("kind") == "agent":
        return "harness", candidate["model_id"]
    url = urlsplit(candidate["connection"]["base_url"])
    endpoint = f"{url.scheme.lower()}://{url.netloc.lower()}{url.path.rstrip('/')}"
    return endpoint, candidate["model_id"]


def require_real_council_supported(session: Session, job: PlanningJob) -> None:
    """Check every candidate before any external call, including a late merger."""
    import sys
    from pathlib import Path

    from agents_ide.adapters.model_catalog import parameters_for
    from agents_ide.services.harness import current_model_metadata

    for member in session.scalars(select(PlanningMember).where(PlanningMember.job_id == job.id)):
        for candidate in json.loads(member.candidates_json):
            if candidate.get("kind") != "agent" or not candidate.get("enabled", True):
                continue
            harness = candidate["harness"]
            path = Path(harness.get("executable_path") or "")
            if (
                sys.platform != "win32"
                or not path.is_absolute()
                or not path.is_file()
                or path.suffix.lower() in {".ps1", ".cmd", ".bat"}
            ):
                raise AppError(
                    "council_harness_real_unverified",
                    "Council требует нативный executable и проверенную изоляцию Windows",
                    422,
                )
            profile = session.get(HarnessProfile, candidate["harness_profile_id"])
            if profile is None or not _harness_is_read_only(profile):
                raise AppError("council_harness_unverified", "Council требует режим чтения", 422)
            parameters_for(
                harness["harness_kind"],
                candidate["model_id"],
                candidate["params"],
                current_model_metadata(profile),
            )


def create_planning_job(
    session: Session, payload: PlanningJobCreate, *, simulated: bool = False
) -> PlanningJob:
    begin_write(session)
    request_body = payload.model_dump(mode="json")
    if not payload.context_paths:
        request_body.pop("context_paths", None)  # Preserve idempotency of existing text-only jobs.
    request_hash = content_hash(request_body)
    existing = session.scalar(
        select(PlanningJob).where(PlanningJob.idempotency_key == payload.idempotency_key)
    )
    if existing:
        if existing.request_hash != request_hash:
            raise AppError("idempotency_conflict", "Planning key belongs to another request", 409)
        if not simulated:
            require_real_council_supported(session, existing)
        return existing
    from agents_ide.operations.maintenance import ensure_available
    from agents_ide.operations.storage import check_capacity, settings_for

    ensure_available(settings_for(session))
    check_capacity(session)
    project = session.get(Project, payload.project_id)
    if project is None or project.archived_at is not None:
        raise AppError("project_unavailable", "Проект недоступен", 409)
    if payload.chat_id:
        chat = session.get(Chat, payload.chat_id)
        if chat is None or chat.project_id != project.id or chat.archived_at is not None:
            raise AppError("chat_unavailable", "Диалог недоступен", 409)
    if not payload.task_text.strip():
        raise AppError("planning_task_empty", "Task must not be blank", 422)
    from agents_ide.services.planning_context import capture_context

    context, read_workspace = capture_context(
        session, project, payload.context_text, payload.context_paths
    )
    job = PlanningJob(
        id=new_id(),
        idempotency_key=payload.idempotency_key,
        request_hash=request_hash,
        project_id=project.id,
        chat_id=payload.chat_id,
        initiator=payload.initiator,
        state="drafting",
        state_version=1,
        task_text=sanitize(payload.task_text),
        read_manifest_hash=content_hash(context),
        context_snapshot_json=_serialise(context),
        read_workspace_json=_serialise(read_workspace),
        budget_json=payload.budget.model_dump_json(),
        usage_json='{"external_calls":0}',
        n_participants_requested=payload.participant_count,
        n_participants_actual=0,
        concurrency=1,
        degraded=False,
        started_at=0,
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    session.add(job)
    session.flush()
    direct_identities: set[tuple[str, str]] = set()
    for index, entry in enumerate(payload.participants):
        selection = entry.selection.model_dump(mode="json", exclude_none=True)
        candidates: list[dict[str, Any]] = []
        member_kind = "llm"
        if selection["kind"] == "group":
            loaded = load_group_snapshot(session, selection["group_id"])
            if loaded is None:
                raise AppError("model_group_unavailable", "Группа недоступна", 409)
            group, rows = loaded
            selection["group_revision"] = group.revision
            selection["group_name"] = group.name
            selection["group_kind"] = group.kind
            if group.kind not in {"llm", "agent"}:
                raise AppError(
                    "council_group_unsupported",
                    "Council requires an LLM or agent group",
                    422,
                )
            member_kind = group.kind
            for row in rows:
                row_payload: dict[str, Any] = {
                    "model_id": row.model_id,
                }
                if row.harness_profile_id:
                    row_payload["harness_profile_id"] = row.harness_profile_id
                else:
                    row_payload["provider_connection_id"] = row.provider_connection_id
                candidates.append(
                    _candidate(
                        session,
                        row_payload,
                        {**json.loads(row.params_json), **entry.params},
                        row.id,
                        row.enabled,
                    )
                )
            if not any(c["enabled"] for c in candidates):
                raise AppError("model_group_empty", "Нужен включённый кандидат", 422)
        else:
            candidate = _candidate(session, selection, entry.params)
            if candidate.get("kind") == "agent":
                member_kind = "agent"
            identity = candidate_identity(candidate)
            if entry.role == "participant" and identity in direct_identities:
                raise AppError(
                    "council_duplicate_member", "Participants must use independent models", 409
                )
            if entry.role == "participant":
                direct_identities.add(identity)
            candidates.append(candidate)
        member = PlanningMember(
            id=new_id(),
            job_id=job.id,
            slot_index=index,
            role=entry.role,
            selection_json=_serialise(selection),
            selection_kind=selection["kind"],
            group_id=selection.get("group_id"),
            harness_profile_id=selection.get("harness_profile_id"),
            provider_connection_id=selection.get("provider_connection_id"),
            model_id=selection.get("model_id", ""),
            params_json=_serialise(entry.params),
            candidates_json=_serialise(candidates),
            status="pending",
        )
        session.add(member)
        _record_event(
            session,
            job.id,
            event_type="planning.member.scheduled",
            member_id=member.id,
            payload={"kind": member_kind},
        )
    _record_event(session, job.id, event_type="planning.created")
    session.flush()
    if not simulated:
        require_real_council_supported(session, job)
    return job


def cancel_planning_job(
    session: Session, job_id: str, payload: PlanningCancelRequest
) -> PlanningJob:
    begin_write(session)
    job = get_planning_job(session, job_id)
    if job.state == "cancelled":
        return job
    if job.state in {"confirmed", "failed"}:
        raise AppError("planning_state_invalid", "Planning is terminal", 409)
    if job.state_version != payload.expected_state_version:
        raise AppError("planning_state_version_invalid", "Expected state version mismatch", 409)
    job.state, job.finished_at = "cancelled", utc_now()
    job.state_version += 1
    job.last_error_json = _serialise(sanitize({"code": "user_cancelled", "reason": payload.reason}))
    _record_event(session, job_id, event_type="planning.cancelled")
    session.flush()
    return job


# Running can remain after a lost worker; it requires explicit unknown-outcome consent.
_RETRY_RESET_STATUSES = {"failed", "unknown", "skipped", "running"}


def effective_candidate(member: PlanningMember, candidate: dict[str, Any]) -> dict[str, Any]:
    """Apply an explicitly recorded access revision to an immutable candidate.

    LLM candidates carry a ``connection`` block keyed by ``provider_connection_id``;
    harness candidates carry a ``harness`` block keyed by ``harness_profile_id``.
    The override storage uses the resource ID so each kind has its own bucket.
    """
    overrides = json.loads(member.access_overrides_json)
    if candidate.get("kind") == "agent":
        key = candidate["harness_profile_id"]
        override = overrides.get(key, {})
        return {**candidate, "harness": {**candidate["harness"], **override}}
    key = candidate["provider_connection_id"]
    override = overrides.get(key, {})
    return {**candidate, "connection": {**candidate["connection"], **override}}


def _refresh_access(session: Session, member: PlanningMember) -> list[dict[str, Any]]:
    overrides = json.loads(member.access_overrides_json)
    changes: list[dict[str, Any]] = []
    for pinned in json.loads(member.candidates_json):
        current = effective_candidate(member, pinned)
        if pinned.get("kind") == "agent":
            profile = session.get(HarnessProfile, pinned["harness_profile_id"])
            if profile is None or profile.archived_at is not None:
                continue
            if not _harness_is_read_only(profile):
                raise AppError(
                    "council_harness_unverified", "Политика harness больше не допускается", 422
                )
            original = pinned["harness"]
            if (
                profile.harness_kind != original["harness_kind"]
                or profile.executable_path != original["executable_path"]
                or json.loads(profile.settings_json) != original["settings"]
            ):
                raise AppError(
                    "planning_retry_harness_changed",
                    "Исполнитель или настройки harness изменились: создайте новый Council",
                    409,
                )
            native_fingerprint = fingerprint(profile.harness_kind, profile.executable_path)
            if profile.version != current["harness"]["version"] or native_fingerprint != current[
                "harness"
            ].get("native_fingerprint"):
                overrides[profile.id] = {
                    "version": profile.version,
                    "native_fingerprint": native_fingerprint,
                }
                changes.append(
                    {
                        "member_id": member.id,
                        "harness_id": profile.id,
                        "previous_version": current["harness"]["version"],
                        "version": profile.version,
                        "native_credentials_refreshed": True,
                    }
                )
            continue
        connection = current["connection"]
        provider = session.get(ProviderConnection, pinned["provider_connection_id"])
        if provider is None or provider.archived_at is not None:
            continue  # An unavailable fallback must not block other pinned candidates.
        if provider.base_url != pinned["connection"]["base_url"]:
            raise AppError(
                "planning_retry_endpoint_changed",
                "Адрес подключения изменён: создайте новый Council",
                409,
                {"connection_id": provider.id},
            )
        if provider.version != connection["version"]:
            overrides[provider.id] = {
                "version": provider.version,
                "secret_reference": provider.secret_reference,
            }
            changes.append(
                {
                    "member_id": member.id,
                    "connection_id": provider.id,
                    "previous_version": connection["version"],
                    "version": provider.version,
                }
            )
    member.access_overrides_json = _serialise(overrides)
    return changes


def retry_planning_job(
    session: Session, job_id: str, payload: PlanningRetryRequest, *, simulated: bool = False
) -> PlanningRetried:
    """Explicit retry with fencing, unchanged budgets and preserved evidence."""
    begin_write(session)
    job = get_planning_job(session, job_id)
    if job.state != "failed":
        raise AppError("planning_state_invalid", "Retry requires a failed planning job", 409)
    if job.state_version != payload.expected_state_version:
        raise AppError("planning_state_version_invalid", "Expected state version mismatch", 409)
    if not simulated:
        require_real_council_supported(session, job)
    now = utc_now()
    if job.lease_owner and (job.lease_expires_at or 0) > now:
        raise AppError("planning_retry_worker_active", "Дождитесь освобождения задания worker", 409)
    from agents_ide.engine.planning_native import processes_stopped

    if not processes_stopped(session, job.id):
        raise AppError(
            "planning_retry_worker_active",
            "Завершение предыдущего дерева процессов не подтверждено",
            409,
        )
    if not job.request_hash:
        raise AppError("legacy_planning_unverifiable", "Создайте новый Council", 409)
    if content_hash(json.loads(job.context_snapshot_json)) != job.read_manifest_hash:
        raise AppError("context_changed", "Контекст Council повреждён", 409)
    budget, usage = json.loads(job.budget_json), json.loads(job.usage_json)
    if job.started_at and now >= job.started_at + budget["max_wallclock_seconds"]:
        raise AppError("planning_deadline_exceeded", "Общий лимит времени исчерпан", 409)
    if usage.get("external_calls", 0) >= budget["max_external_calls"]:
        raise AppError("planning_budget_exhausted", "Общий лимит вызовов исчерпан", 409)
    members = list(
        session.scalars(
            select(PlanningMember)
            .where(PlanningMember.job_id == job.id)
            .order_by(PlanningMember.slot_index)
        )
    )
    if not members:
        raise AppError("planning_invalid", "Planning job has no members", 409)
    reset_indices = (
        {m.slot_index for m in members if m.status in _RETRY_RESET_STATUSES}
        if payload.reset_all_failed
        else set(payload.reset_member_indices)
    )
    if reset_indices - {m.slot_index for m in members}:
        raise AppError("planning_retry_invalid", "Unknown member index", 409)
    for member in members:
        if member.slot_index in reset_indices and member.status not in _RETRY_RESET_STATUSES:
            raise AppError("planning_retry_invalid", "Only unsuccessful members may be reset", 409)
        if member.status in {"unknown", "running"} and member.slot_index not in reset_indices:
            raise AppError("planning_retry_unresolved", "Resolve all unknown outcomes", 409)
    if not reset_indices and not any(m.status == "pending" for m in members):
        raise AppError("planning_retry_invalid", "No work to retry", 409)
    unfinished = list(
        session.scalars(
            select(PlanningAttempt).where(
                PlanningAttempt.job_id == job.id, PlanningAttempt.outcome == "running"
            )
        )
    )
    unknown_ids = {m.id for m in members if m.status in {"unknown", "running"}}
    unknown_ids.update(a.member_id for a in unfinished)
    if unknown_ids and not payload.acknowledge_unknown_result:
        raise AppError(
            "planning_retry_unknown_requires_ack",
            "Предыдущий вызов мог выполниться у провайдера. Подтвердите возможный повтор и оплату",
            409,
        )
    # Fencing occurs before requeueing. A late old worker cannot finish or release this round.
    job.generation += 1
    job.lease_owner, job.lease_expires_at = None, None
    for attempt in unfinished:
        attempt.outcome, attempt.error_code, attempt.finished_at = "unknown", "worker_lost", now
    reset, preserved, access_changes = [], [], []
    for member in members:
        if member.slot_index in reset_indices:
            member.status = "pending"
            member.candidate_index = 0
            # Keep the format-repair count and diagnostic drafts across retry rounds.
            member.started_at = member.finished_at = None
            member.error_json = None
            reset.append(member.id)
        else:
            preserved.append(member.id)
        if payload.refresh_credentials and member.status == "pending":
            access_changes.extend(_refresh_access(session, member))
    job.state, job.finished_at, job.last_error_json = "drafting", None, None
    job.state_version += 1
    job.updated_at = now
    _record_event(
        session,
        job_id,
        event_type="planning.retried",
        payload={
            "reset_members": reset,
            "preserved_members": preserved,
            "reason": payload.reason,
            "generation": job.generation,
            "external_calls": usage.get("external_calls", 0),
            "acknowledged_unknown_members": sorted(unknown_ids),
            "access_changes": access_changes,
        },
    )
    session.flush()
    return PlanningRetried(
        state=PlanningState(job.state),
        state_version=job.state_version,
        reset_member_ids=reset,
        preserved_member_ids=preserved,
    )


# Council ends in 'failed' with this code when fewer than two participants
# produced an accepted draft. The user may explicitly accept the lone
# accepted draft as the plan; that path is recorded as a 'single_member'
# revision and never as a successful Council consensus.
_QUORUM_LOST_CODES = {"council_quorum_missing"}


def promote_single_member_plan(
    session: Session, job_id: str, payload: PlanningPromoteSingleRequest
) -> PlanningPromoteSingle:
    """Promote the single accepted draft after a quorum loss into a revision.

    The job must be in ``failed`` with ``last_error.code`` equal to
    ``council_quorum_missing`` (or any code in ``_QUORUM_LOST_CODES``) and
    must have exactly one accepted draft. The user must explicitly set
    ``confirm_degraded`` to acknowledge that this is not a Council consensus.

    The new revision uses ``author='single_member'`` so the audit trail and
    downstream consumers (RunStart, snapshot, UI) can detect the degraded
    source. Failed/invalid drafts remain untouched and remain available for
    diagnostics.
    """
    begin_write(session)
    job = get_planning_job(session, job_id)
    if job.state != "failed":
        raise AppError("planning_state_invalid", "Single-member promotion needs a failed job", 409)
    if job.state_version != payload.expected_state_version:
        raise AppError("planning_state_version_invalid", "Expected state version mismatch", 409)
    last_error = json.loads(job.last_error_json) if job.last_error_json else {}
    if last_error.get("code") not in _QUORUM_LOST_CODES:
        raise AppError(
            "planning_quorum_required",
            "Single-member promotion requires a quorum loss",
            409,
        )
    if job.lease_owner and (job.lease_expires_at or 0) > utc_now():
        raise AppError("planning_worker_active", "Planning worker has not released the job", 409)
    if not job.request_hash:
        raise AppError("legacy_planning_unverifiable", "Legacy planning source is unverified", 409)
    if content_hash(json.loads(job.context_snapshot_json)) != job.read_manifest_hash:
        raise AppError("context_changed", "Pinned planning context changed", 409)
    members = list(
        session.scalars(
            select(PlanningMember)
            .where(PlanningMember.job_id == job.id)
            .order_by(PlanningMember.slot_index)
        )
    )
    participants = [m for m in members if m.role == "participant"]
    accepted_members = [m for m in participants if m.status == "succeeded"]
    if len(accepted_members) != 1:
        raise AppError(
            "planning_single_member_count",
            "Exactly one accepted draft is required",
            409,
            {"n_accepted": len(accepted_members)},
        )
    accepted_member = accepted_members[0]
    draft = session.scalar(
        select(PlanningDraft).where(PlanningDraft.member_id == accepted_member.id)
    )
    if (
        draft is None
        or draft.job_id != job.id
        or not draft.accepted
        or draft.parse_status != "found"
        or draft.body_truncated
        or draft.content_hash != content_hash(draft.body_text)
        or draft.byte_length != len(draft.body_text.encode("utf-8"))
    ):
        raise AppError(
            "planning_single_member_invalid",
            "The accepted draft is missing or invalid",
            409,
        )
    try:
        document = parse_document(draft.body_text)
    except ValueError as exc:
        raise AppError(
            "planning_single_member_invalid", "The accepted draft is invalid", 409
        ) from exc
    # Promotion consumes persisted evidence, not another external attempt. Expired
    # budgets are allowed, but unfinished work cannot be silently bypassed.
    if any(m.status in {"pending", "running", "unknown"} for m in participants) or session.scalar(
        select(PlanningAttempt.id)
        .where(PlanningAttempt.job_id == job.id, PlanningAttempt.outcome == "running")
        .limit(1)
    ):
        raise AppError("planning_work_unresolved", "Planning work is still unresolved", 409)
    revision = add_revision(session, job, document, author="single_member")
    job.merged_by_member_id = accepted_member.id
    job.degraded = True
    job.n_participants_actual = 1
    job.last_error_json = None
    job.finished_at = None
    job.generation += 1
    job.lease_owner, job.lease_expires_at = None, None
    # The state move is owned by add_revision: 'needs_answers' if questions
    # remain in the lone draft, otherwise 'ready_for_confirmation'.
    _record_event(
        session,
        job_id,
        event_type="planning.single_member_promoted",
        member_id=accepted_member.id,
        payload={
            "revision_number": revision.revision_number,
            "n_participants_actual": 1,
            "model_id": accepted_member.model_id,
            "draft_id": draft.id,
            "draft_hash": draft.content_hash,
            "generation": job.generation,
        },
    )
    session.flush()
    return PlanningPromoteSingle(
        state=PlanningState(job.state),
        state_version=job.state_version,
        revision_number=revision.revision_number,
        degraded=job.degraded,
        n_participants_actual=job.n_participants_actual,
        accepted_member_id=accepted_member.id,
        accepted_model_id=accepted_member.model_id or None,
    )


def revision_hash(revision: PlanningRevision) -> str:
    return content_hash(
        {
            "job_id": revision.job_id,
            "revision_number": revision.revision_number,
            "body_text": revision.body_text,
            "plan": json.loads(revision.plan_json),
            "questions": json.loads(revision.questions_json),
            "answers": json.loads(revision.answers_json),
        }
    )


def add_revision(
    session: Session,
    job: PlanningJob,
    document: PlanDocument,
    *,
    author: str = "merger",
    answers: list[dict[str, Any]] | None = None,
) -> PlanningRevision:
    # The persisted document must remain readable under the same byte limit.
    try:
        parse_document(document.model_dump_json())
    except ValueError as exc:
        raise AppError("planning_document_invalid", "Plan exceeds the document limit", 422) from exc
    number = _next_revision_number(session, job.id)
    if number > 16:
        raise AppError("planning_revision_limit", "Council allows up to 16 revisions", 409)
    questions = [q.model_dump() for q in document.questions]
    revision = PlanningRevision(
        id=new_id(),
        job_id=job.id,
        revision_number=number,
        author=author,
        body_text=document.body_text,
        body_truncated=False,
        parse_status="found",
        plan_json=document.model_dump_json(),
        questions_json=_serialise(questions),
        answers_json=_serialise(answers or []),
        questions_hash=content_hash(questions),
        readiness="needs_answers" if questions else "ready",
        created_at=utc_now(),
        answered_hash=content_hash(answers) if answers else None,
    )
    revision.confirmation_hash = revision_hash(revision)
    session.add(revision)
    job.state = "needs_answers" if questions else "ready_for_confirmation"
    job.state_version += 1
    job.updated_at = utc_now()
    session.flush()
    return revision


def submit_answers(
    session: Session, job_id: str, payload: PlanningAnswersSubmit
) -> PlanningAnswersAccepted:
    begin_write(session)
    job = get_planning_job(session, job_id)
    previous = latest_revision(session, job_id)
    if job.state not in {"needs_answers", "ready_for_confirmation"}:
        raise AppError("planning_state_invalid", "Planning cannot be revised now", 409)
    if previous is None or previous.revision_number != payload.expected_revision:
        raise AppError(
            "planning_revision_conflict", "Reload the latest revision before answering", 409
        )
    original = parse_document(previous.plan_json)
    questions = {q.id: q for q in original.questions}
    by_id = {a.question_id: a for a in payload.answers}
    if len(by_id) != len(payload.answers) or set(by_id) != set(questions):
        raise AppError(
            "planning_answers_incomplete", "Answer each current question exactly once", 422
        )
    resolved = []
    for qid, question in questions.items():
        answer = by_id[qid]
        allowed = {o.id: o.label for o in question.options}
        if answer.kind != question.kind or any(
            i not in allowed for i in answer.selected_option_ids
        ):
            raise AppError("planning_answer_invalid", "Answer does not match the question", 422)
        if answer.free_text and question.kind != "text" and not question.allow_text:
            raise AppError(
                "planning_answer_invalid", "This choice question does not accept text", 422
            )
        resolved.append(
            {
                **answer.model_dump(),
                "prompt": question.prompt,
                "selected_labels": [allowed[i] for i in answer.selected_option_ids],
            }
        )
    if not resolved and payload.user_body_text is None:
        raise AppError("planning_no_changes", "No answers or edits supplied", 422)
    answers = json.loads(previous.answers_json) + sanitize(resolved)
    try:
        if payload.user_body_text is not None:
            document = parse_document(sanitize(payload.user_body_text))
        else:
            # Answers are part of the exact text presented for explicit confirmation.
            appendix = "\n\nОтветы пользователя:\n" + "\n".join(
                f"- {a['prompt']}: {', '.join(a['selected_labels'])} {a['free_text'] or ''}"
                for a in resolved
            )
            document = PlanDocument(
                body_text=original.body_text + appendix, steps=original.steps, questions=[]
            )
    except ValueError as exc:
        raise AppError(
            "planning_document_invalid", "Edited plan must be a complete valid JSON plan", 422
        ) from exc
    revision = add_revision(session, job, document, author="user", answers=answers)
    for answer in payload.answers:
        session.add(
            PlanningAnswer(
                id=new_id(),
                revision_id=revision.id,
                question_id=answer.question_id,
                kind=answer.kind,
                selected_option_ids_json=_serialise(answer.selected_option_ids),
                free_text=sanitize(answer.free_text or ""),
                created_at=utc_now(),
            )
        )
    _record_event(
        session,
        job_id,
        event_type="planning.answered",
        payload={"revision_number": revision.revision_number},
    )
    return PlanningAnswersAccepted(
        revision_number=revision.revision_number,
        questions_hash=revision.questions_hash,
        readiness=PlanningRevisionReadiness(revision.readiness),
        requires_merger_revisit=False,
        new_revision=_revision_to_view(revision),
    )


def checked_revision(
    session: Session, job_id: str, number: int, hash_value: str, *, confirmed: bool = False
) -> tuple[PlanningJob, PlanningRevision]:
    job = get_planning_job(session, job_id)
    revision = latest_revision(session, job_id)
    if revision is None or revision.revision_number != number:
        raise AppError("planning_revision_conflict", "Expected latest planning revision", 409)
    if (
        revision.readiness != "ready"
        or revision.body_truncated
        or not revision.plan_json
        or revision.plan_json == "{}"
    ):
        raise AppError("planning_revision_invalid", "Plan is incomplete or unverified", 409)
    if revision_hash(revision) != hash_value or revision.confirmation_hash != hash_value:
        raise AppError(
            "planning_confirm_hash_invalid", "Plan content or confirmation hash changed", 409
        )
    try:
        document = parse_document(revision.plan_json)
        if (
            document.questions
            or json.loads(revision.questions_json)
            or document.body_text != revision.body_text
        ):
            raise ValueError("Unresolved questions or inconsistent plan")
    except ValueError as exc:
        raise AppError(
            "planning_revision_invalid", "Plan is incomplete or unverified", 409
        ) from exc
    if confirmed and (job.state != "confirmed" or revision.confirmed_at is None):
        raise AppError("planning_revision_unconfirmed", "Planning revision is not confirmed", 409)
    return job, revision


def confirm_planning(
    session: Session, job_id: str, payload: PlanningConfirmRequest
) -> PlanningConfirmed:
    begin_write(session)
    job, revision = checked_revision(
        session, job_id, payload.expected_revision, payload.confirmation_hash
    )
    if job.state not in {"ready_for_confirmation", "confirmed"}:
        raise AppError("planning_state_invalid", "Plan is not ready for confirmation", 409)
    if job.state != "confirmed":
        revision.confirmed_at = utc_now()
        job.state, job.finished_at = "confirmed", utc_now()
        job.state_version += 1
        _record_event(
            session,
            job_id,
            event_type="planning.confirmed",
            payload={"revision_number": revision.revision_number},
        )
    return PlanningConfirmed(
        revision_number=revision.revision_number,
        confirmation_hash=payload.confirmation_hash,
        confirmed_at=cast(datetime, _dt(revision.confirmed_at)),
    )


def _revision_to_view(revision: PlanningRevision) -> PlanningRevisionView:
    plan = json.loads(revision.plan_json or "{}")
    items = [{"id": f"P{i}", **step} for i, step in enumerate(plan.get("steps", []), 1)]
    return PlanningRevisionView(
        id=revision.id,
        revision_number=revision.revision_number,
        author=cast(Literal["merger", "user", "single_member"], revision.author),
        body_text=sanitize(revision.body_text),
        body_truncated=revision.body_truncated,
        parse=cast(Literal["found", "none_found", "invalid_format"], revision.parse_status),
        readiness=PlanningRevisionReadiness(revision.readiness),
        questions=json.loads(revision.questions_json),
        plan_items=items,
        answers=json.loads(revision.answers_json or "[]"),
        confirmation_hash=revision.confirmation_hash,
        answered_hash=revision.answered_hash,
        answered=bool(revision.answered_hash),
        created_at=cast(datetime, _dt(revision.created_at)),
        confirmed_at=_dt(revision.confirmed_at),
    )


def load_planning_view(session: Session, job_id: str) -> PlanningJobView:
    session.flush()
    job = get_planning_job(session, job_id)
    members = list(
        session.scalars(
            select(PlanningMember)
            .where(PlanningMember.job_id == job_id)
            .order_by(PlanningMember.slot_index)
        )
    )
    by_id = {m.id: m for m in members}
    drafts = []
    for d in session.scalars(select(PlanningDraft).where(PlanningDraft.job_id == job_id)):
        m = by_id[d.member_id]
        drafts.append(
            {
                "member_id": m.id,
                "slot_index": m.slot_index,
                "role": m.role,
                "selection_kind": m.selection_kind,
                "harness_profile_id": m.harness_profile_id,
                "provider_connection_id": m.provider_connection_id,
                "model_id": m.model_id,
                "status": m.status,
                "body_text": sanitize(d.body_text),
                "body_truncated": d.body_truncated,
                "byte_length": d.byte_length,
                "parse_status": d.parse_status,
                "accepted": d.accepted,
                "content_hash": d.content_hash,
            }
        )
    return PlanningJobView(
        id=job.id,
        project_id=job.project_id,
        chat_id=job.chat_id,
        idempotency_key=job.idempotency_key,
        initiator=job.initiator,
        state=PlanningState(job.state),
        state_version=job.state_version,
        task_text=job.task_text,
        read_manifest_hash=job.read_manifest_hash,
        n_participants_requested=job.n_participants_requested,
        n_participants_actual=job.n_participants_actual,
        degraded=job.degraded,
        budget=json.loads(job.budget_json),
        usage=json.loads(job.usage_json),
        members=[
            PlanningMemberView.model_validate(
                {
                    "id": m.id,
                    "slot_index": m.slot_index,
                    "role": m.role,
                    "selection": json.loads(m.selection_json),
                    "status": m.status,
                    "selected_member_id": m.actual_member_id,
                    "selected_model_id": m.model_id,
                    "selected_connection_id": m.provider_connection_id,
                    "selected_connection_name": next(
                        (
                            (c.get("connection") or {}).get("name")
                            for c in json.loads(m.candidates_json)
                            if c.get("provider_connection_id") == m.provider_connection_id
                        ),
                        None,
                    ),
                    "selected_harness_id": m.harness_profile_id,
                    "selected_harness_name": next(
                        (
                            (c.get("harness") or {}).get("name")
                            for c in json.loads(m.candidates_json)
                            if c.get("harness_profile_id") == m.harness_profile_id
                        ),
                        None,
                    ),
                    "selected_harness_kind": next(
                        (
                            (c.get("harness") or {}).get("harness_kind")
                            for c in json.loads(m.candidates_json)
                            if c.get("harness_profile_id") == m.harness_profile_id
                        ),
                        None,
                    ),
                    "started_at": _dt(m.started_at),
                    "finished_at": _dt(m.finished_at),
                    "error": json.loads(m.error_json) if m.error_json else None,
                }
            )
            for m in members
        ],
        drafts=[PlanningDraftView.model_validate(d) for d in drafts],
        events=list(
            reversed(
                [
                    PlanningEventView(
                        sequence=e.sequence,
                        type=e.type,
                        member_id=e.member_id,
                        payload=json.loads(e.payload_json),
                    )
                    for e in session.scalars(
                        select(PlanningEvent)
                        .where(PlanningEvent.job_id == job.id)
                        .order_by(PlanningEvent.sequence.desc())
                        .limit(200)
                    )
                ]
            )
        ),
        revisions=[
            _revision_to_view(r)
            for r in session.scalars(
                select(PlanningRevision)
                .where(PlanningRevision.job_id == job_id)
                .order_by(PlanningRevision.revision_number)
            )
        ],
        last_error=json.loads(job.last_error_json) if job.last_error_json else None,
        started_at=_dt(job.started_at),
        created_at=cast(datetime, _dt(job.created_at)),
        updated_at=cast(datetime, _dt(job.updated_at)),
        finished_at=_dt(job.finished_at),
    )


def list_planning_jobs(
    session: Session, *, project_id: str | None, chat_id: str | None, include_completed: bool
) -> list[PlanningJobView]:
    query = select(PlanningJob)
    if project_id:
        query = query.where(PlanningJob.project_id == project_id)
    if chat_id:
        query = query.where(PlanningJob.chat_id == chat_id)
    if not include_completed:
        query = query.where(PlanningJob.state.not_in(["confirmed", "cancelled", "failed"]))
    return [
        load_planning_view(session, j.id)
        for j in session.scalars(query.order_by(PlanningJob.created_at.desc()).limit(100))
    ]


def confirmation_hash_for_view(
    session: Session, job_id: str, revision_number: int
) -> dict[str, Any]:
    get_planning_job(session, job_id)
    revision = latest_revision(session, job_id)
    if revision is None or revision.revision_number != revision_number:
        raise AppError("planning_revision_conflict", "Expected latest revision", 409)
    return {
        "job_id": job_id,
        "revision_number": revision_number,
        "questions_hash": revision.questions_hash,
        "confirmation_hash": revision_hash(revision),
        "readiness": revision.readiness,
    }


def parse_revision_body(body_text: str) -> tuple[str, list[dict[str, Any]], str]:
    try:
        plan = parse_document(body_text)
    except ValueError:
        return "invalid_format", [], "invalid_format"
    questions = [q.model_dump() for q in plan.questions]
    return "found", questions, "needs_answers" if questions else "ready"


def planning_inputs(
    session: Session, source: PlanningSource, project_id: str, inputs: dict[str, Any]
) -> dict[str, Any]:
    job, revision = checked_revision(
        session, source.job_id, source.revision_number, source.confirmation_hash, confirmed=True
    )
    if job.project_id != project_id:
        raise AppError("planning_project_mismatch", "Council plan belongs to another project", 409)
    document = parse_document(revision.plan_json)
    provenance = {
        "degraded": job.degraded,
        "n_participants_requested": job.n_participants_requested,
        "n_participants_actual": job.n_participants_actual,
        "revision_author": revision.author,
        "source_member_id": job.merged_by_member_id,
    }
    pinned = {
        "task": job.task_text,
        "plan": revision.body_text,
        "plan_items": document.items(),
        "planning_text": revision.body_text,
        "planning_questions": json.loads(revision.questions_json),
        "planning_answers": json.loads(revision.answers_json),
        "planning_revision_hash": revision.confirmation_hash,
        "planning_provenance": provenance,
    }
    if any(k in inputs and inputs[k] != v for k, v in pinned.items()):
        raise AppError(
            "planning_inputs_conflict", "Confirmed planning inputs cannot be overridden", 409
        )
    return {**inputs, **pinned}
