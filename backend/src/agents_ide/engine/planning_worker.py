"""Leased Council execution. No network calls hold SQLite's write lock."""

from __future__ import annotations

import json
import threading
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from agents_ide.adapters.base import AdapterError, ExternalOutcome, LLMAdapterRequest, LLMResult
from agents_ide.adapters.fake import FakeLLMAdapter, parse_fake_scenario
from agents_ide.adapters.llm_http import HttpLLMAdapter
from agents_ide.domain.common import content_hash, new_id, utc_now
from agents_ide.domain.contracts import PlanningState
from agents_ide.domain.planning_document import PlanDocument, parse_document
from agents_ide.domain.planning_prompt import plan_merge_prompt, plan_participant_prompt
from agents_ide.engine.artifacts import sanitize
from agents_ide.errors import AppError
from agents_ide.persistence.models import (
    PlanningAttempt,
    PlanningDraft,
    PlanningJob,
    PlanningMember,
    ProviderConnection,
)
from agents_ide.security.secrets import SecretStore
from agents_ide.services import planning as service
from agents_ide.services.transactions import begin_write

LEASE_SECONDS = 30
ACTIVE = {"drafting", "merging"}


@dataclass(frozen=True)
class PlanningClaim:
    job_id: str
    owner: str
    generation: int


@dataclass
class DispatchResult:
    final_state: PlanningState
    error: dict[str, Any] | None = None


def _fail(session: Session, job: PlanningJob, code: str) -> None:
    job.state, job.finished_at = "failed", utc_now()
    job.state_version += 1
    job.last_error_json = service._serialise({"code": code})
    service._record_event(session, job.id, event_type="planning.failed", payload={"code": code})


def claim_planning_job(
    factory: sessionmaker[Session], owner: str, job_id: str | None = None
) -> PlanningClaim | None:
    with factory() as session:
        begin_write(session)
        query = select(PlanningJob).where(
            PlanningJob.state.in_(ACTIVE),
            or_(PlanningJob.lease_expires_at.is_(None), PlanningJob.lease_expires_at <= utc_now()),
        )
        if job_id:
            query = query.where(PlanningJob.id == job_id)
        job = session.scalar(query.order_by(PlanningJob.created_at).limit(1))
        if job is None:
            return None
        unfinished = list(
            session.scalars(
                select(PlanningAttempt).where(
                    PlanningAttempt.job_id == job.id, PlanningAttempt.outcome == "running"
                )
            )
        )
        for attempt in unfinished:
            attempt.outcome, attempt.error_code, attempt.finished_at = (
                "unknown",
                "worker_lost",
                utc_now(),
            )
            member = session.get(PlanningMember, attempt.member_id)
            assert member
            member.status, member.error_json = "unknown", '{"code":"unknown_external_result"}'
        if unfinished or not job.request_hash:
            _fail(
                session,
                job,
                "unknown_external_result" if unfinished else "legacy_planning_unverifiable",
            )
            session.commit()
            return None
        job.lease_owner, job.lease_expires_at = owner, utc_now() + LEASE_SECONDS
        job.generation += 1
        if not job.started_at:
            job.started_at = utc_now()
        claim = PlanningClaim(job.id, owner, job.generation)
        session.commit()
        return claim


def _owned(session: Session, claim: PlanningClaim) -> PlanningJob:
    job = service.get_planning_job(session, claim.job_id)
    if (
        job.lease_owner != claim.owner
        or job.generation != claim.generation
        or (job.lease_expires_at or 0) <= utc_now()
    ):
        raise AppError("planning_lease_lost", "Planning ownership lost", 409)
    return job


def _budget_reason(job: PlanningJob) -> str | None:
    budget, usage = json.loads(job.budget_json), json.loads(job.usage_json)
    if utc_now() - job.started_at >= budget["max_wallclock_seconds"]:
        return "planning_deadline_exceeded"
    if usage.get("external_calls", 0) >= budget["max_external_calls"]:
        return "planning_budget_exhausted"
    return None


def _prepare(
    factory: sessionmaker[Session], claim: PlanningClaim
) -> tuple[str, dict[str, Any], LLMAdapterRequest] | None:
    with factory() as session:
        begin_write(session)
        job = _owned(session, claim)
        if job.state not in ACTIVE:
            return None
        members = list(
            session.scalars(
                select(PlanningMember)
                .where(PlanningMember.job_id == job.id)
                .order_by(PlanningMember.slot_index)
            )
        )
        participants = [m for m in members if m.role == "participant"]
        accepted = [m for m in participants if m.status == "succeeded"]
        job.n_participants_actual = len(accepted)
        job.degraded = len(accepted) < job.n_participants_requested
        member = (
            next((m for m in participants if m.status == "pending"), None)
            if job.state == "drafting"
            else None
        )
        if member is None:
            if len(accepted) < 2:
                _fail(session, job, "council_quorum_missing")
                session.commit()
                return None
            if job.state == "drafting":
                job.state = "merging"
                job.state_version += 1
            member = next(m for m in members if m.role == "merger")
            if member.status != "pending":
                _fail(session, job, "merge_unavailable")
                session.commit()
                return None
        if reason := _budget_reason(job):
            _fail(session, job, reason)
            session.commit()
            return None
        candidates = json.loads(member.candidates_json)
        identities = set()
        for other in accepted:
            if other.id != member.id:
                selected = next(
                    (
                        c
                        for c in json.loads(other.candidates_json)
                        if c["model_id"] == other.model_id
                        and c["provider_connection_id"] == other.provider_connection_id
                    ),
                    None,
                )
                if selected:
                    identities.add(service.candidate_identity(selected))
        candidate = None
        while member.candidate_index < len(candidates):
            current = candidates[member.candidate_index]
            resource = session.get(ProviderConnection, current["provider_connection_id"])
            reason = None
            if not current["enabled"]:
                reason = "disabled"
            elif member.role == "participant" and service.candidate_identity(current) in identities:
                reason = "duplicate_model"
            elif resource is None or resource.archived_at is not None:
                reason = "connection_unavailable"
            elif resource.version != current["connection"]["version"]:
                reason = "resource_changed"
            if reason:
                service._record_event(
                    session,
                    job.id,
                    member_id=member.id,
                    event_type="planning.candidate.skipped",
                    payload={
                        "index": member.candidate_index,
                        "reason": reason,
                        "model_id": current["model_id"],
                        "connection_id": current["provider_connection_id"],
                    },
                )
                member.candidate_index += 1
            else:
                candidate = current
                break
        if candidate is None:
            member.status, member.finished_at = "failed", utc_now()
            member.error_json = '{"code":"model_group_exhausted"}'
            if member.role == "merger":
                _fail(session, job, "merge_failed")
            session.commit()
            return None
        context = json.loads(job.context_snapshot_json).get("context_section", "")
        if content_hash(json.loads(job.context_snapshot_json)) != job.read_manifest_hash:
            _fail(session, job, "context_changed")
            session.commit()
            return None
        if member.role == "merger":
            drafts = [
                {"member_id": d.member_id, "body_text": d.body_text}
                for d in session.scalars(
                    select(PlanningDraft).where(
                        PlanningDraft.job_id == job.id, PlanningDraft.accepted.is_(True)
                    )
                )
            ]
            prompt = plan_merge_prompt(job.task_text, drafts, context)
        else:
            prompt = plan_participant_prompt(job.task_text, context)
        previous = list(
            session.scalars(
                select(PlanningAttempt)
                .where(PlanningAttempt.member_id == member.id)
                .order_by(PlanningAttempt.attempt_index)
            )
        )
        if previous and previous[-1].outcome == "invalid_format":
            prompt += (
                "\nYour previous answer was invalid. Return the complete JSON schema "
                "with nonempty steps and explicit questions."
            )
        attempt = PlanningAttempt(
            id=new_id(),
            job_id=job.id,
            member_id=member.id,
            attempt_index=len(previous) + 1,
            generation=claim.generation,
            candidate_json=service._serialise(candidate),
            outcome="running",
            started_at=utc_now(),
        )
        session.add(attempt)
        member.status, member.started_at = "running", utc_now()
        member.actual_member_id, member.model_id = candidate["member_id"], candidate["model_id"]
        member.provider_connection_id = candidate["provider_connection_id"]
        usage = json.loads(job.usage_json)
        usage["external_calls"] = usage.get("external_calls", 0) + 1
        job.usage_json = service._serialise(usage)
        job.updated_at = utc_now()
        service._record_event(
            session,
            job.id,
            member_id=member.id,
            event_type="planning.member.started",
            payload={
                "attempt_id": attempt.id,
                "model_id": member.model_id,
                "candidate_index": member.candidate_index,
                "connection_id": member.provider_connection_id,
            },
        )
        node = (
            "council_merger"
            if member.role == "merger"
            else f"council_participant_{member.slot_index}"
        )
        request = LLMAdapterRequest(
            role=node,
            model_id=member.model_id,
            prompt=prompt,
            context_package={"__node_id__": node, "read_manifest_hash": job.read_manifest_hash},
            params=candidate["params"],
            response_format="json",
            output_schema=PlanDocument.model_json_schema(),
            attempt_index=attempt.attempt_index,
            visit_index=1,
            deadline_at=job.started_at + json.loads(job.budget_json)["max_wallclock_seconds"],
        )
        session.commit()
        return attempt.id, candidate, request


def _finish(
    factory: sessionmaker[Session], claim: PlanningClaim, attempt_id: str, result: LLMResult
) -> None:
    with factory() as session:
        begin_write(session)
        job = _owned(session, claim)
        attempt = session.get(PlanningAttempt, attempt_id)
        assert attempt
        member = session.get(PlanningMember, attempt.member_id)
        assert member
        raw = str(sanitize(result.raw_text))
        encoded = raw.encode("utf-8")
        truncated = len(encoded) > 65536
        body = encoded[:65536].decode("utf-8", errors="ignore")
        attempt.body_text, attempt.finished_at = body, utc_now()
        attempt.outcome = result.outcome.value
        attempt.error_code = result.error.code if result.error else None
        usage = json.loads(job.usage_json)
        usage["wallclock_seconds"] = utc_now() - job.started_at
        for name, value in (
            ("tokens_used", result.tokens_used),
            ("cost_estimated", result.cost_estimated),
        ):
            if value is not None:
                usage[name] = usage.get(name, 0) + value
        job.usage_json = service._serialise(usage)
        member.finished_at = utc_now()
        document = None
        if result.outcome == ExternalOutcome.SUCCEEDED and not truncated:
            with suppress(ValueError):
                document = parse_document(body)
        if result.outcome == ExternalOutcome.SUCCEEDED and document is None:
            attempt.outcome, attempt.error_code = "invalid_format", "invalid_format"
        if job.state not in ACTIVE:
            member.status = "unknown"
            member.error_json = '{"code":"cancelled_during_call"}'
        elif utc_now() >= job.started_at + json.loads(job.budget_json)["max_wallclock_seconds"]:
            member.status = "unknown"
            _fail(session, job, "planning_deadline_exceeded")
        elif document is not None:
            member.status, member.error_json = "succeeded", None
            if member.role == "merger":
                service.add_revision(session, job, document)
                job.merged_by_member_id = member.id
            else:
                job.n_participants_actual += 1
        elif result.outcome in {
            ExternalOutcome.UNKNOWN,
            ExternalOutcome.TRANSPORT_DROPPED,
            ExternalOutcome.PROCESS_DIED,
        }:
            member.status = "unknown"
            member.error_json = '{"code":"unknown_external_result"}'
            _fail(session, job, "unknown_external_result")
        elif (
            attempt.error_code == "invalid_format"
            or result.outcome == ExternalOutcome.INVALID_FORMAT
        ) and member.draft_revision < 1:
            member.draft_revision += 1
            member.status = "pending"  # one format repair; consumes the same shared budget
        elif (
            result.outcome in {ExternalOutcome.UNAVAILABLE, ExternalOutcome.RETRYABLE_FAILURE}
            and result.no_effect
            and result.error is not None
            and result.error.retry_safety == "safe"
        ) or (
            result.outcome == ExternalOutcome.PERMISSION_DENIED
            and attempt.error_code == "provider_unauthorized"
        ):
            member.candidate_index += 1
            member.status = "pending"
            member.error_json = service._serialise({"code": attempt.error_code or attempt.outcome})
        else:
            # Configuration/invalid answers are not reasons to silently change models.
            member.status = "failed"
            member.error_json = service._serialise({"code": attempt.error_code or attempt.outcome})
            if member.role == "merger":
                _fail(session, job, "merge_failed")
        # Keep the latest diagnostic draft; every attempt retains its own bounded body.
        if member.role == "participant":
            draft = session.scalar(
                select(PlanningDraft).where(PlanningDraft.member_id == member.id)
            )
            if draft is None:
                draft = PlanningDraft(
                    id=new_id(), job_id=job.id, member_id=member.id, created_at=utc_now()
                )
                session.add(draft)
            draft.accepted = document is not None and member.status == "succeeded"
            draft.parse_status = "found" if document else "invalid_format"
            draft.body_text, draft.body_truncated = body, truncated
            draft.byte_length, draft.content_hash = len(body.encode("utf-8")), content_hash(body)
        service._record_event(
            session,
            job.id,
            member_id=member.id,
            event_type="planning.attempt.finished",
            payload={
                "attempt_id": attempt.id,
                "outcome": attempt.outcome,
                "error_code": attempt.error_code,
            },
        )
        session.commit()


def dispatch_planning_job(
    factory: sessionmaker[Session],
    job_id: str,
    *,
    simulated: bool = False,
    fake_scenario: dict[str, Any] | None = None,
    abort: threading.Event | None = None,
    secret_store: SecretStore | None = None,
    claim: PlanningClaim | None = None,
) -> DispatchResult:
    abort = abort or threading.Event()
    claim = claim or claim_planning_job(factory, new_id(), job_id)
    if claim is None:
        with factory() as session:
            job = service.get_planning_job(session, job_id)
            return DispatchResult(PlanningState(job.state))
    call_stop, finished = threading.Event(), threading.Event()

    def check_owned() -> None:
        with factory() as session:
            job = _owned(session, claim)
            if job.state not in ACTIVE or abort.is_set() or call_stop.is_set():
                raise AppError("planning_interrupted", "Planning interrupted", 409)

    def monitor() -> None:
        while not finished.wait(0.2):
            try:
                with factory() as session:
                    begin_write(session)
                    job = _owned(session, claim)
                    if (
                        job.state not in ACTIVE
                        or abort.is_set()
                        or utc_now()
                        >= job.started_at + json.loads(job.budget_json)["max_wallclock_seconds"]
                    ):
                        call_stop.set()
                    job.lease_expires_at = utc_now() + LEASE_SECONDS
                    session.commit()
            except Exception:
                call_stop.set()
                return

    watcher = threading.Thread(target=monitor, daemon=True)
    watcher.start()
    try:
        while not abort.is_set() and not call_stop.is_set():
            prepared = _prepare(factory, claim)
            if prepared is None:
                with factory() as session:
                    if service.get_planning_job(session, job_id).state not in ACTIVE:
                        break
                continue
            attempt_id, candidate, request = prepared
            try:
                from dataclasses import replace

                connection = dict(candidate["connection"])
                if not simulated and connection.get("secret_reference"):
                    if secret_store is None:
                        raise AppError("secret_unavailable", "No credential store", 409)
                    connection["secret_value"] = secret_store.get(connection["secret_reference"])
                request = replace(
                    request, connection=connection, stop_event=call_stop, check_owned=check_owned
                )
                adapter = (
                    FakeLLMAdapter(parse_fake_scenario(fake_scenario or {}))
                    if simulated
                    else HttpLLMAdapter()
                )
                result = adapter.run(request)
            except AppError as exc:
                result = LLMResult(
                    ExternalOutcome.UNAVAILABLE
                    if exc.code == "secret_unavailable"
                    else ExternalOutcome.UNKNOWN,
                    "",
                    None,
                    None,
                    error=AdapterError(
                        exc.code,
                        "Planning call unavailable",
                        "safe" if exc.code == "secret_unavailable" else "unknown",
                    ),
                    no_effect=exc.code == "secret_unavailable",
                )
            except Exception:
                result = LLMResult(
                    ExternalOutcome.UNKNOWN,
                    "",
                    None,
                    None,
                    error=AdapterError("unknown_external_result", "Planning call outcome unknown"),
                )
            _finish(factory, claim, attempt_id, result)
    finally:
        finished.set()
        watcher.join(timeout=2)
        with factory() as session:
            begin_write(session)
            job = service.get_planning_job(session, job_id)
            if job.lease_owner == claim.owner and job.generation == claim.generation:
                job.lease_owner, job.lease_expires_at = None, None
                if job.state in ACTIVE and call_stop.is_set():
                    _fail(session, job, "planning_interrupted")
            session.commit()
    with factory() as session:
        job = service.get_planning_job(session, job_id)
        return DispatchResult(
            PlanningState(job.state),
            json.loads(job.last_error_json) if job.last_error_json else None,
        )
