"""Checkpoints for UI-only tests: no model or worker is launched."""

import json
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from agents_ide.config import Settings
from agents_ide.domain.common import new_id, to_json, utc_now
from agents_ide.engine.events import append_event
from agents_ide.engine.loops import loop_body_nodes
from agents_ide.persistence.database import create_database
from agents_ide.persistence.models import AgentSession, Run, StepAttempt, StepExecution
from agents_ide.services.run_messages import claim_message, finish_message
from agents_ide.services.transactions import begin_write

data_dir, run_id, mode = sys.argv[1:]
engine = create_database(Settings(data_dir=Path(data_dir)))
with Session(engine) as session:
    begin_write(session)
    run = session.get(Run, run_id)
    if mode in {"historical-failed", "historical-waiting"}:
        run.state = "failed" if mode == "historical-failed" else "waiting_input"
        run.state_version += 1
        run.waiting_reason_json = to_json(
            {
                "code": "configuration_invalid",
                "allowed_actions": ["resolve", "resume", "cancel"],
            }
        )
    elif mode in {"context", "context-compacted"}:
        runtime = json.loads(run.runtime_json)
        runtime.setdefault("agent_context_usage", {})[run.current_node_id] = {
            "attempt_id": run.current_attempt_id,
            "tokens": 232000 if mode == "context" else 48000,
        }
        run.runtime_json = to_json(runtime)
    elif mode == "loop-counts":
        runtime = json.loads(run.runtime_json)
        runtime["loop_counts"] = {"repair": 2}
        run.runtime_json = to_json(runtime)
        run.state_version += 1
    elif mode == "deliver":
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
    elif mode == "resume-unavailable":
        # Resume failed during availability checks, before a new attempt. Keep
        # the prior attempt and recovery audit, as in the production regression.
        run.state = "waiting_input"
        run.state_version += 1
        run.waiting_reason_json = to_json(
            {
                "code": "session_resume_unavailable",
                "details": {"reason": "resource_changed"},
                "allowed_actions": ["resolve", "resume", "pause", "stop", "cancel"],
            }
        )
        run.resume_target_json = to_json(
            {
                "action": "retry_attempt",
                "node_id": run.current_node_id,
                "execution_id": run.current_execution_id,
                "blockers": ["session_resume_unavailable"],
            }
        )
    elif mode in {"waiting", "waiting-recovery"}:
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
        if mode == "waiting-recovery":
            attempt = session.get(StepAttempt, run.current_attempt_id)
            attempt.finished_at = utc_now()
            candidate = json.loads(run.snapshot_json)["dependencies"]["nodes"][run.current_node_id][
                "candidates"
            ][0]
            attempt.selection_json = to_json({**candidate, "member_id": candidate["id"]})
            runtime = json.loads(run.runtime_json)
            runtime.update(
                candidate_index=0,
                current_agent_session={
                    "execution_id": run.current_execution_id,
                    "session_key": "saved",
                    "member_index": 0,
                },
                native_sessions={
                    "saved": {
                        "session_id": "saved-session",
                        "server_version": "fixture",
                    }
                },
            )
            run.runtime_json = to_json(runtime)
            session.add(
                AgentSession(
                    id=new_id(),
                    attempt_id=attempt.id,
                    harness_kind="codex",
                    role="reviewer",
                    external_session_id="saved-session",
                    started_at=utc_now(),
                    finished_at=utc_now(),
                )
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
        node_id = "first" if mode in {"first", "loop-next"} else "second"
        for attempt in session.scalars(
            select(StepAttempt).join(StepExecution).where(StepExecution.run_id == run_id)
        ):
            attempt.status = "succeeded"
        for execution in session.scalars(
            select(StepExecution).where(StepExecution.run_id == run_id)
        ):
            execution.status = "succeeded"
        execution_id, attempt_id = run_id[:20] + node_id, run_id[:20] + node_id + "a"
        runtime = json.loads(run.runtime_json or "{}")
        cycle = 1
        if mode == "loop-next":
            cycle = runtime["cycle_id"] + 1
            execution_id, attempt_id = new_id(), new_id()
            for completed in ("start", "route"):
                session.add(
                    StepExecution(
                        id=new_id(),
                        run_id=run_id,
                        node_id=completed,
                        visit_index=1,
                        cycle_id=cycle - 1,
                        scope="main",
                        status="succeeded",
                    )
                )
            graph = json.loads(run.snapshot_json)["graph"]
            edge = next(edge for edge in graph["edges"] if edge.get("loop"))
            runtime["loop_observation_cycles"] = dict.fromkeys(loop_body_nodes(graph, edge), cycle)
            runtime["loop_counts"]["repair"] += 1
        session.add(
            StepExecution(
                id=execution_id,
                run_id=run_id,
                node_id=node_id,
                visit_index=2 if mode == "loop-next" else 1,
                cycle_id=cycle,
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
        runtime.update(cycle_id=cycle, next_node_id=node_id, work={"scope": "main"})
        run.runtime_json = to_json(runtime)
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
            {"text": "Checking the first files" if node_id == "first" else "Reviewing the result"},
            node_id=node_id,
            execution_id=execution_id,
            attempt_id=attempt_id,
        )
    run.updated_at = utc_now()
    session.commit()
engine.dispose()
