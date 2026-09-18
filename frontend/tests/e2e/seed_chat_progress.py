"""Checkpoints for UI-only tests: no model or worker is launched."""

import sys
from pathlib import Path

from agents_ide.config import Settings
from agents_ide.domain.common import to_json, utc_now
from agents_ide.engine.events import append_event
from agents_ide.persistence.database import create_database
from agents_ide.persistence.models import Run, StepAttempt, StepExecution
from agents_ide.services.run_messages import claim_message, finish_message
from agents_ide.services.transactions import begin_write
from sqlalchemy import select
from sqlalchemy.orm import Session

data_dir, run_id, mode = sys.argv[1:]
engine = create_database(Settings(data_dir=Path(data_dir)))
with Session(engine) as session:
    begin_write(session)
    run = session.get(Run, run_id)
    if mode == "deliver":
        message = claim_message(session, run, run.current_attempt_id)
        assert message
        if message.get("question_id") or message.get("permission_id"):
            append_event(
                session,
                run_id,
                "agent.input_closed",
                {
                    "question_id": message.get("question_id")
                    or f"permission:{message['permission_id']}"
                },
                node_id=run.current_node_id,
                execution_id=run.current_execution_id,
                attempt_id=run.current_attempt_id,
            )
        finish_message(
            session,
            run,
            run.current_attempt_id,
            {"command_id": message["command_id"], "delivered": True},
        )
    elif mode.startswith("stream-"):
        append_event(
            session,
            run_id,
            "attempt.text_delta",
            {"text": "\n" + "\n".join(f"{mode} line {i}" for i in range(60))},
            node_id=run.current_node_id,
            execution_id=run.current_execution_id,
            attempt_id=run.current_attempt_id,
        )
    elif mode == "tools":
        for status in ["pending", "running", "running", "completed"]:
            append_event(
                session,
                run_id,
                "agent.tool_call",
                {
                    "session_id": "ses_test",
                    "call_id": "read_1",
                    "tool": "read",
                    "status": status,
                    "summary": "status.md",
                },
                node_id=run.current_node_id,
                execution_id=run.current_execution_id,
                attempt_id=run.current_attempt_id,
            )
    elif mode == "repeated-tools":
        for index in range(4):
            append_event(
                session,
                run_id,
                "agent.tool_call",
                {"call_id": f"generic_{index}"},
                node_id=run.current_node_id,
                execution_id=run.current_execution_id,
                attempt_id=run.current_attempt_id,
            )
    elif mode == "permission":
        append_event(
            session,
            run_id,
            "agent.input_requested",
            {
                "question_id": "permission:p1",
                "permission_id": "p1",
                "kind": "permission",
                "permission": "bash",
                "patterns": ["npm test"],
                "questions": [],
            },
            node_id=run.current_node_id,
            execution_id=run.current_execution_id,
            attempt_id=run.current_attempt_id,
        )
    elif mode == "question":
        append_event(
            session,
            run_id,
            "agent.input_requested",
            {
                "question_id": "q1",
                "questions": [
                    {
                        "id": "file",
                        "question": "Which file should I check?",
                        "options": ["README.md"],
                    }
                ],
            },
            node_id=run.current_node_id,
            execution_id=run.current_execution_id,
            attempt_id=run.current_attempt_id,
        )
    elif mode == "waiting":
        run.state = "waiting_input"
        run.state_version += 1
        session.get(StepAttempt, run.current_attempt_id).status = "unknown"
        run.waiting_reason_json = to_json(
            {
                "code": "unknown_external_result",
                "details": {},
                "allowed_actions": ["resolve", "stop", "cancel"],
            }
        )
    elif mode in {"stopped", "paused"}:
        run.state = mode
        run.state_version += 1
        session.get(StepAttempt, run.current_attempt_id).status = "interrupted"
        session.get(StepExecution, run.current_execution_id).status = "interrupted"
        run.resume_target_json = to_json(
            {"action": "retry_attempt", "node_id": run.current_node_id, "blockers": []}
        )
    else:
        node_id = "first" if mode == "first" else "second"
        for attempt in session.scalars(
            select(StepAttempt)
            .join(StepExecution)
            .where(StepExecution.run_id == run_id)
        ):
            attempt.status = "succeeded"
        for execution in session.scalars(
            select(StepExecution).where(StepExecution.run_id == run_id)
        ):
            execution.status = "succeeded"
        execution_id, attempt_id = run_id[:20] + node_id, run_id[:20] + node_id + "a"
        session.add(
            StepExecution(
                id=execution_id,
                run_id=run_id,
                node_id=node_id,
                visit_index=1,
                cycle_id=1,
                scope="main",
                status="running",
                attempt_count=1,
            )
        )
        session.flush()
        session.add(
            StepAttempt(
                id=attempt_id,
                execution_id=execution_id,
                attempt_index=1,
                status="running",
            )
        )
        session.flush()
        run.state = "running"
        run.state_version += 1
        run.current_node_id, run.current_execution_id, run.current_attempt_id = (
            node_id,
            execution_id,
            attempt_id,
        )
        append_event(
            session,
            run_id,
            "node.entered",
            {},
            node_id=node_id,
            execution_id=execution_id,
        )
        append_event(
            session,
            run_id,
            "attempt.text_delta",
            {
                "text": "Checking the first files"
                if node_id == "first"
                else "Reviewing the result"
            },
            node_id=node_id,
            execution_id=execution_id,
            attempt_id=attempt_id,
        )
    run.updated_at = utc_now()
    session.commit()
engine.dispose()
