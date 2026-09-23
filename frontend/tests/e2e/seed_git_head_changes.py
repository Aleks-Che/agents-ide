"""Reproduce an old dispatch_next HEAD guard in the browser-test database."""

import json
import sys
from pathlib import Path

from sqlalchemy.orm import Session

from agents_ide.config import Settings
from agents_ide.domain.common import to_json
from agents_ide.engine import git_commit as git
from agents_ide.engine.git_process import run_git
from agents_ide.persistence.database import create_database
from agents_ide.persistence.models import Run
from agents_ide.services.transactions import begin_write

data_dir, run_id = sys.argv[1:]
engine = create_database(Settings(data_dir=Path(data_dir)))
with Session(engine) as session:
    begin_write(session)
    run = session.get(Run, run_id)
    workspace = Path(json.loads(run.snapshot_json)["workspace"]["workspace_path"])
    baseline = git.capture_baseline(workspace, run_id, ["src/**"])
    run_git(workspace, ["rm", "src/__pycache__/cache.pyc"])
    run_git(workspace, ["commit", "-qm", "remove generated cache"])
    (workspace / "src").mkdir(exist_ok=True)
    (workspace / "src/draft.txt").write_text("unfinished work\n", encoding="utf-8", newline="\n")
    run.current_node_id = "agent"
    run.current_execution_id = run.current_attempt_id = None
    run.state, run.state_version = "waiting_input", run.state_version + 1
    run.waiting_reason_json = to_json(
        {
            "code": "external_change_detected",
            "details": {"reason": "external_change_detected", "details": {}},
            "allowed_actions": ["resolve", "resume", "stop", "cancel"],
        }
    )
    run.resume_target_json = to_json(
        {
            "action": "dispatch_next",
            "node_id": "agent",
            "execution_id": None,
            "blockers": ["external_change_detected"],
        }
    )
    runtime = json.loads(run.runtime_json)
    runtime["git"] = {
        "baseline": baseline.to_dict(),
        "head": baseline.head_sha,
        "branch": baseline.branch,
        "allowlist": ["src/**"],
        "phase": "ready",
    }
    run.runtime_json = to_json(runtime)
    session.commit()
engine.dispose()
