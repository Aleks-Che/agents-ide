"""Leased Council execution. No network calls hold SQLite's write lock."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from agents_ide.adapters.base import (
    AdapterError,
    AgentAdapterRequest,
    ExternalOutcome,
    LLMAdapterRequest,
    LLMResult,
)
from agents_ide.adapters.fake import FakeAgentAdapter, FakeLLMAdapter, parse_fake_scenario
from agents_ide.adapters.llm_http import HttpLLMAdapter
from agents_ide.domain.common import content_hash, new_id, utc_now
from agents_ide.domain.contracts import PlanningState
from agents_ide.domain.planning_document import PlanDocument, parse_document
from agents_ide.domain.planning_prompt import plan_merge_prompt, plan_participant_prompt
from agents_ide.engine.artifacts import sanitize
from agents_ide.errors import AppError
from agents_ide.persistence.models import (
    HarnessProfile,
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


def _matching_candidate(other: PlanningMember) -> dict[str, Any] | None:
    """Pick the persisted candidate that produced ``other``'s accepted draft.

    The match key is the actual model id plus the resource id (harness profile
    or provider connection) so we avoid regressing to legacy connection-only
    matching for harness participants.
    """
    candidates: list[dict[str, Any]] = json.loads(other.candidates_json)
    for c in candidates:
        if c["model_id"] != other.model_id:
            continue
        if c.get("kind") == "agent":
            if c.get("harness_profile_id") == other.harness_profile_id:
                return c
        elif c.get("provider_connection_id") == other.provider_connection_id:
            return c
    return None


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
    factory: sessionmaker[Session], claim: PlanningClaim, *, simulated: bool = False
) -> tuple[str, dict[str, Any], LLMAdapterRequest | AgentAdapterRequest] | None:
    with factory() as session:
        begin_write(session)
        job = _owned(session, claim)
        if job.state not in ACTIVE:
            return None
        from agents_ide.operations.storage import check_capacity

        check_capacity(session)
        if not simulated:
            try:
                service.require_real_council_supported(session, job)
            except AppError as exc:
                _fail(session, job, exc.code)
                session.commit()
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
                selected = _matching_candidate(other)
                if selected:
                    identities.add(service.candidate_identity(selected))
        candidate = None
        while member.candidate_index < len(candidates):
            current = service.effective_candidate(member, candidates[member.candidate_index])
            if current.get("kind") == "agent":
                profile = session.get(HarnessProfile, current["harness_profile_id"])
                reason = None
                if not current["enabled"]:
                    reason = "disabled"
                elif (
                    member.role == "participant"
                    and service.candidate_identity(current) in identities
                ):
                    reason = "duplicate_model"
                elif profile is None or profile.archived_at is not None:
                    reason = "harness_unavailable"
                elif profile.version != current["harness"]["version"]:
                    reason = "resource_changed"
                elif current["harness"].get("native_fingerprint"):
                    from agents_ide.security.native_credentials import fingerprint

                    if (
                        fingerprint(profile.harness_kind, profile.executable_path)
                        != current["harness"]["native_fingerprint"]
                    ):
                        reason = "native_credentials_changed"
                resource_id = current["harness_profile_id"]
            else:
                resource = session.get(ProviderConnection, current["provider_connection_id"])
                reason = None
                if not current["enabled"]:
                    reason = "disabled"
                elif (
                    member.role == "participant"
                    and service.candidate_identity(current) in identities
                ):
                    reason = "duplicate_model"
                elif resource is None or resource.archived_at is not None:
                    reason = "connection_unavailable"
                elif resource.version != current["connection"]["version"]:
                    reason = "resource_changed"
                resource_id = current["provider_connection_id"]
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
                        "kind": current.get("kind", "llm"),
                        "resource_id": resource_id,
                        "harness_id": current.get("harness_profile_id"),
                        "connection_id": current.get("provider_connection_id"),
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
        drafts: list[dict[str, Any]] = []
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
        if candidate.get("kind") == "agent":
            member.harness_profile_id = candidate["harness_profile_id"]
            member.provider_connection_id = None
        else:
            member.harness_profile_id = None
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
                "kind": candidate.get("kind", "llm"),
                "harness_id": member.harness_profile_id,
                "connection_id": member.provider_connection_id,
            },
        )
        node = (
            "council_merger"
            if member.role == "merger"
            else f"council_participant_{member.slot_index}"
        )
        deadline_at = job.started_at + json.loads(job.budget_json)["max_wallclock_seconds"]
        if candidate.get("kind") == "agent":
            adapter_request: AgentAdapterRequest | LLMAdapterRequest = AgentAdapterRequest(
                role=node,
                model_id=member.model_id,
                prompt=prompt,
                context_package={
                    "__node_id__": node,
                    "context": context,
                    "drafts": drafts,
                    "read_manifest_hash": job.read_manifest_hash,
                    "read_workspace_mode": json.loads(job.read_workspace_json or "{}").get(
                        "mode", "no_workspace_access"
                    ),
                },
                # The native runtime supplies a private copy, never the live repository.
                workspace_path="",
                capabilities={
                    "harness_kind": candidate["harness"]["harness_kind"],
                    "output_schema": PlanDocument.native_output_schema(),
                },
                params=candidate["params"],
                attempt_index=attempt.attempt_index,
                visit_index=1,
                deadline_at=deadline_at,
            )
        else:
            adapter_request = LLMAdapterRequest(
                role=node,
                model_id=member.model_id,
                prompt=prompt,
                context_package={"__node_id__": node, "read_manifest_hash": job.read_manifest_hash},
                params=candidate["params"],
                response_format="json",
                output_schema=PlanDocument.model_json_schema(),
                attempt_index=attempt.attempt_index,
                visit_index=1,
                deadline_at=deadline_at,
            )
        session.commit()
        return attempt.id, candidate, adapter_request


def _finish(
    factory: sessionmaker[Session], claim: PlanningClaim, attempt_id: str, result: LLMResult
) -> None:
    with factory() as session:
        begin_write(session)
        job = _owned(session, claim)
        attempt = session.get(PlanningAttempt, attempt_id)
        assert attempt
        if attempt.generation != claim.generation or attempt.outcome != "running":
            raise AppError("planning_attempt_stale", "Planning attempt already settled", 409)
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
            usage[name + "_complete"] = usage.get(name + "_complete", True) and value is not None
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


def _invoke_external(
    candidate: dict[str, Any],
    request: LLMAdapterRequest | AgentAdapterRequest,
    *,
    simulated: bool,
    fake_scenario: dict[str, Any] | None,
    secret_store: SecretStore | None,
    stop_event: threading.Event,
    check_owned: Callable[[], None],
    native_supervisor: Any = None,
    attempt_id: str | None = None,
) -> LLMResult:
    """Dispatch outside the transaction; simulations use the shared adapter contract."""
    check_owned()
    if candidate.get("kind") == "agent":
        assert isinstance(request, AgentAdapterRequest)
        result = _invoke_harness(
            replace(request, stop_event=stop_event, check_owned=check_owned),
            simulated=simulated,
            fake_scenario=fake_scenario,
            candidate=candidate,
            native_supervisor=native_supervisor,
            attempt_id=attempt_id,
        )
        check_owned()
        return result
    assert isinstance(request, LLMAdapterRequest)
    connection = dict(candidate["connection"])
    if not simulated and connection.get("secret_reference"):
        if secret_store is None:
            raise AppError("secret_unavailable", "No credential store", 409)
        connection["secret_value"] = secret_store.get(connection["secret_reference"])
    llm_request = replace(
        request, connection=connection, stop_event=stop_event, check_owned=check_owned
    )
    llm_scenario = {
        key: value for key, value in (fake_scenario or {}).items() if not key.startswith("harness_")
    }
    adapter = FakeLLMAdapter(parse_fake_scenario(llm_scenario)) if simulated else HttpLLMAdapter()
    return adapter.run(llm_request)


def _invoke_harness(
    request: AgentAdapterRequest,
    *,
    simulated: bool,
    fake_scenario: dict[str, Any] | None,
    candidate: dict[str, Any] | None = None,
    native_supervisor: Any = None,
    attempt_id: str | None = None,
) -> LLMResult:
    """Both execution paths use the same agent result and Council validation."""
    if not simulated:
        from agents_ide.engine.planning_native import run_native

        if candidate is None or native_supervisor is None or attempt_id is None:
            raise AppError(
                "council_harness_real_unverified", "Planning process owner required", 422
            )
        return _agent_result(run_native(candidate, request, native_supervisor, attempt_id))
    scenario = dict(fake_scenario or {})
    responses = list(scenario.get("responses", []))
    node_id = request.role
    body_key = f"harness_body_text:{node_id}"
    outcome_key = f"harness_outcome:{node_id}"
    explicit_body = scenario.get(body_key, scenario.get("harness_body_text"))
    explicit_outcome = scenario.get(outcome_key, scenario.get("harness_outcome"))
    matching = next(
        (
            r
            for r in responses
            if r.get("node_id") == node_id
            and r.get("attempt_index", 1) == request.attempt_index
            and r.get("visit_index") in (None, request.visit_index)
        ),
        None,
    )
    if matching is None or explicit_body is not None or explicit_outcome is not None:
        response = dict(matching or {})
        if matching is not None:
            responses.remove(matching)
        response.update(node_id=node_id, attempt_index=request.attempt_index)
        response.setdefault(
            "raw_text",
            json.dumps(
                {
                    "body_text": f"Simulated plan from {request.model_id}",
                    "steps": [{"title": "Implement", "acceptance_criteria": ["Tests pass"]}],
                    "questions": [],
                },
                ensure_ascii=False,
            ),
        )
        if explicit_body is not None:
            response["raw_text"] = explicit_body
        if explicit_outcome is not None:
            response["outcome"] = explicit_outcome
        if "harness_error_code" in scenario:
            response["error_code"] = scenario["harness_error_code"]
        responses.append(response)
    # No workspace passed: even a scripted file edit is refused by FakeAgentAdapter.
    result = FakeAgentAdapter(parse_fake_scenario({"responses": responses})).run(request)
    return _agent_result(result)


def _agent_result(result: Any) -> LLMResult:
    return LLMResult(
        outcome=result.outcome,
        raw_text=result.raw_text,
        validated_result=result.validated_result,
        decision=result.decision,
        error=result.error,
        tokens_used=result.tokens_used,
        cost_estimated=result.cost_estimated,
        budget_quality=result.budget_quality,
        elapsed_seconds=result.elapsed_seconds,
        finished_at=result.finished_at,
        no_effect=result.no_effect,
        result_schema="harness_council",
    )


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
    from agents_ide.engine.planning_native import PlanningSupervisor
    from agents_ide.worker.processes import ProcessRegistry

    native_supervisor = PlanningSupervisor(
        factory, ProcessRegistry(), job_id, claim.owner, claim.generation
    )

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
            prepared = _prepare(factory, claim, simulated=simulated)
            if prepared is None:
                with factory() as session:
                    if service.get_planning_job(session, job_id).state not in ACTIVE:
                        break
                continue
            attempt_id, candidate, request = prepared
            try:
                result = _invoke_external(
                    candidate,
                    request,
                    simulated=simulated,
                    fake_scenario=fake_scenario,
                    secret_store=secret_store,
                    stop_event=call_stop,
                    check_owned=check_owned,
                    native_supervisor=native_supervisor,
                    attempt_id=attempt_id,
                )
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
        native_supervisor.stop()
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
