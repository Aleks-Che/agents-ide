"""Audited JSON-processing overrides for an already paused execution."""

import json
from typing import Any

from jsonschema import Draft202012Validator
from sqlalchemy.orm import Session

from agents_ide.domain.common import to_json
from agents_ide.domain.schemas import RunCommand
from agents_ide.engine import artifacts
from agents_ide.engine.json_response import parse_json_response
from agents_ide.engine.run_configuration import effective_snapshot
from agents_ide.errors import AppError
from agents_ide.persistence.models import ArtifactManifest, Run, StepAttempt


def prepare_json_reprocessing(session: Session, run: Run, command: RunCommand) -> dict[str, Any]:
    from agents_ide.worker.processes import stored_processes_stopped

    processing = command.payload["json_processing"]
    if (
        set(command.payload) - {"json_processing", "reason"}
        or not isinstance(processing, dict)
        or set(processing) - {"strip_thinking_tags", "extract_json"}
        or any(type(value) is not bool for value in processing.values())
        or not any(processing.values())
    ):
        raise AppError("resolution_invalid", "Выберите обработку JSON-ответа", 422)
    runtime, snapshot = json.loads(run.runtime_json), effective_snapshot(run)
    waiting = json.loads(run.waiting_reason_json or "{}") or runtime.get("waiting_reason", {})
    config = snapshot.get("dependencies", {}).get("nodes", {}).get(run.current_node_id, {})
    attempt = session.get(StepAttempt, run.current_attempt_id) if run.current_attempt_id else None
    if (
        waiting.get("code") != "invalid_response_format"
        or not (config.get("response_format") == "json" or config.get("output_schema"))
        or attempt is None
        or attempt.execution_id != run.current_execution_id
        or attempt.status != "failed"
        or attempt.external_outcome != "succeeded"
    ):
        raise AppError("resolution_invalid", "Нет сохранённого JSON-ответа для обработки", 409)
    if not stored_processes_stopped(session, run):
        raise AppError("reconciliation_required", "Дождитесь остановки прежнего исполнителя", 409)
    artifact = session.get(ArtifactManifest, attempt.result_artifact_id)
    if artifact is None or artifact.run_id != run.id or artifact.step_attempt_id != attempt.id:
        raise AppError("result_missing", "Сохранённый ответ недоступен", 409)
    body = json.loads(artifact.body_json or "{}")
    raw = body.get("raw_text")
    if not isinstance(raw, str):
        raise AppError("result_missing", "Полный текст ответа не сохранён", 409)
    try:
        value = parse_json_response(raw, **processing)
    except (ValueError, RecursionError):
        raise AppError(
            "resolution_invalid", "Из сохранённого ответа не удалось извлечь JSON", 422
        ) from None
    if not isinstance(value, dict) or (
        config.get("output_schema")
        and not Draft202012Validator(config["output_schema"]).is_valid(value)
    ):
        raise AppError("resolution_invalid", "Извлечённый JSON не соответствует схеме узла", 422)
    # The original snapshot and response remain immutable. The worker repeats
    # the full result validation, including plan/evidence checks, before advancing.
    resolution = artifacts.record_artifact(
        session,
        run.id,
        artifacts.ArtifactPayload(
            "resolution_data",
            body={
                "json_processing": processing,
                "source_artifact_id": artifact.id,
                "attempt_id": attempt.id,
            },
        ),
        source_kind="api",
        step_execution_id=run.current_execution_id,
        step_attempt_id=attempt.id,
        cycle_id=run.current_cycle_id,
    )
    resolution.source_ref = command.command_id
    runtime.setdefault("resolution_artifact_ids", []).append(resolution.id)
    runtime.setdefault("json_processing_overrides", {})[run.current_node_id] = processing
    runtime["json_reprocessing"] = {
        "attempt_id": attempt.id,
        "source_artifact_id": artifact.id,
        "command_id": command.command_id,
    }
    runtime.pop("retry_resolution", None)
    run.runtime_json = to_json(runtime)
    return {"applied": True, "reprocessing_pending": True, "artifact_id": resolution.id}
