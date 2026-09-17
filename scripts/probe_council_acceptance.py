"""Review or explicitly run three native Council calls using synthetic input only."""

import argparse
import json
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from agents_ide.api.app import create_app
from agents_ide.config import Settings
from agents_ide.engine import planning_worker
from agents_ide.engine.planning_native import processes_stopped
from agents_ide.persistence.models import PlanningAttempt, PlanningEvent
from agents_ide.security.filesystem import atomic_write, private_directory

TASK = (
    "Prepare a short plan for a stable ascending sort of integer lists. "
    "Empty input must work and duplicates must be retained. Return the required "
    "JSON with at most two steps and questions: []. Only plan; do not execute "
    "commands or write files."
)
MODELS = ["gpt-5.6-sol", "gpt-5.6-luna", "minimax-coding-plan/MiniMax-M3"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", required=True, type=Path)
    parser.add_argument("--opencode", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="New private directory")
    parser.add_argument("--allow-model-calls", type=int, choices=[3])
    args = parser.parse_args()
    if args.allow_model_calls is None:
        print(json.dumps({"model_calls": 0, "proposed_limit": 3, "models": MODELS, "task": TASK}))
        return 0
    if args.output.exists():
        parser.error("Choose a new output directory; prior calls must not be replayed")
    for executable in (args.codex, args.opencode):
        if not executable.is_absolute() or not executable.is_file():
            parser.error("Supply an absolute native executable path")
    private_directory(args.output)
    workspace = args.output / "workspace"
    workspace.mkdir()
    (workspace / "notes.md").write_text(TASK, encoding="utf-8")
    ledger_path = args.output / "calls.json"
    calls: list[str] = []
    original = planning_worker._invoke_external

    def bounded_invoke(candidate, *positional, **keyword):
        if len(calls) >= 3:
            raise RuntimeError("Explicit native call budget exhausted")
        calls.append(candidate["model_id"])
        atomic_write(ledger_path, json.dumps({"limit": 3, "calls": calls}).encode())
        return original(candidate, *positional, **keyword)

    planning_worker._invoke_external = bounded_invoke
    settings = Settings(data_dir=args.output / "data", port=18765, frontend_dir=args.output / "ui")
    app = create_app(settings)
    try:
        with TestClient(app, base_url=settings.origin) as client:
            paired = client.post(
                "/api/auth/pair",
                json={"code": (settings.data_dir / "runtime/pair-code").read_text()},
                headers={"Origin": settings.origin},
            )
            headers = {"Origin": settings.origin, "X-CSRF-Token": paired.json()["csrf_token"]}

            def post(path, body):
                response = client.post("/api/" + path, json=body, headers=headers)
                response.raise_for_status()
                return response.json()

            project = post(
                "projects", {"name": "Synthetic Council", "workspace_path": str(workspace)}
            )
            profiles = {}
            for kind, executable, policy in (
                ("codex", args.codex, "read_only"),
                ("opencode", args.opencode, "no_tools"),
            ):
                profiles[kind] = post(
                    "harness_profiles",
                    {
                        "name": kind,
                        "harness_kind": kind,
                        "executable_path": str(executable),
                        "settings": {"permission_mode": policy},
                    },
                )
            group = post(
                "model_groups/agent",
                {
                    "name": "heavy merger",
                    "members": [
                        {"model_id": MODELS[2], "harness_profile_id": profiles["opencode"]["id"]}
                    ],
                },
            )
            participants = [
                {
                    "role": "participant",
                    "selection": {
                        "kind": "direct",
                        "harness_profile_id": profiles["codex"]["id"],
                        "model_id": model,
                    },
                }
                for model in MODELS[:2]
            ]
            participants.append(
                {"role": "merger", "selection": {"kind": "group", "group_id": group["id"]}}
            )
            job = post(
                "planning_jobs",
                {
                    "project_id": project["id"],
                    "task_text": TASK,
                    "context_paths": ["notes.md"],
                    "participants": participants,
                    "budget": {
                        "max_external_calls": 3,
                        "max_wallclock_seconds": 300,
                        "concurrency": 1,
                    },
                    "idempotency_key": uuid.uuid4().hex,
                },
            )
            result = planning_worker.dispatch_planning_job(app.state.session_factory, job["id"])
            view = client.get("/api/planning_jobs/" + job["id"]).json()
            with app.state.session_factory() as session:
                attempts = list(
                    session.scalars(
                        select(PlanningAttempt).where(PlanningAttempt.job_id == job["id"])
                    )
                )
                events = list(
                    session.scalars(select(PlanningEvent).where(PlanningEvent.job_id == job["id"]))
                )
                runtime = [json.loads(attempt.runtime_json) for attempt in attempts]
                report = {
                    "state": str(result.final_state),
                    "model_calls": len(calls),
                    "models": calls,
                    "usage": view.get("usage"),
                    "outcomes": [
                        {"outcome": a.outcome, "error_code": a.error_code} for a in attempts
                    ],
                    "distinct_sessions": len(
                        {item.get("session_id") for item in runtime if item.get("session_id")}
                    ),
                    "all_processes_stopped": processes_stopped(session, job["id"]),
                    "native_events": sum(
                        event.type == "planning.agent.native_event" for event in events
                    ),
                    "accepted_drafts": sum(draft["accepted"] for draft in view.get("drafts", [])),
                    "revision_count": len(view.get("revisions", [])),
                }
            atomic_write(args.output / "report.json", json.dumps(report, indent=2).encode())
            print(json.dumps(report))
            return (
                0
                if report["state"] == "ready_for_confirmation" and report["all_processes_stopped"]
                else 1
            )
    finally:
        planning_worker._invoke_external = original


if __name__ == "__main__":
    raise SystemExit(main())
