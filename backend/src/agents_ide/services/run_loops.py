"""Audited adjustment of future loop iterations without interrupting an agent."""

import json
from typing import Any

from sqlalchemy.orm import Session

from agents_ide.domain.common import new_id, to_json, utc_now
from agents_ide.domain.schemas import RunCommand
from agents_ide.engine.events import append_event
from agents_ide.engine.loops import loop_progress
from agents_ide.engine.run_configuration import effective_snapshot
from agents_ide.errors import AppError
from agents_ide.persistence.models import Run, RunPolicyRevision

ADJUSTABLE_STATES = {
    "queued",
    "running",
    "retry_wait",
    "pause_requested",
    "paused",
    "stopped",
    "waiting_input",
}


def adjust_loop(session: Session, run: Run, command: RunCommand) -> dict[str, Any]:
    payload = command.payload
    if (
        set(payload) != {"loop_key", "delta"}
        or not isinstance(payload.get("loop_key"), str)
        or type(payload.get("delta")) is not int
        or payload["delta"] == 0
    ):
        raise AppError("loop_adjustment_invalid", "Нужны loop_key и ненулевое целое delta", 422)
    runtime = json.loads(run.runtime_json)
    snapshot = effective_snapshot(run)
    loop = next(
        (v for v in loop_progress(snapshot["graph"], runtime) if v["key"] == payload["loop_key"]),
        None,
    )
    if loop is None:
        raise AppError("loop_adjustment_invalid", "Цикл или текущий пункт плана изменился", 409)
    # Older unlimited runs may already exceed the configured loop budget.
    # Adding one always grants one more transition, without erasing usage.
    limit = max(loop["max_iterations"], loop["completed"]) + payload["delta"]
    if limit < loop["completed"]:
        raise AppError("loop_adjustment_invalid", "Нельзя уменьшить остаток ниже нуля", 422)
    runtime.setdefault("loop_limits", {})[loop["key"]] = limit
    run.runtime_json = to_json(runtime)
    response = {
        "loop_key": loop["key"],
        "completed": loop["completed"],
        "remaining": limit - loop["completed"],
        "max_iterations": limit,
    }
    session.add(
        RunPolicyRevision(
            id=new_id(),
            run_id=run.id,
            limit_name=f"loop:{loop['key']}",
            old_value_json=to_json(loop["max_iterations"]),
            new_value_json=to_json(limit),
            reason="adjust_loop",
            author="api",
            created_at=utc_now(),
        )
    )
    append_event(
        session,
        run.id,
        "budget.updated",
        response,
        command_id=command.command_id,
        simulated=snapshot.get("execution_mode") == "simulated",
    )
    return response
