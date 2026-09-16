"""Bounded-history browser fixture, confined to the disposable e2e database."""

import sys
from pathlib import Path

from sqlalchemy import URL, create_engine, delete
from sqlalchemy.orm import Session

from agents_ide.engine.artifacts import ArtifactPayload, record_artifact
from agents_ide.engine.events import append_event
from agents_ide.persistence.models import Run, RunEvent

directory = Path(sys.argv[1]).resolve()
local = Path(__file__).resolve().parents[3] / ".local"
assert directory.is_relative_to(local.resolve()) and directory.name.startswith("e2e-")
engine = create_engine(URL.create("sqlite", database=str(directory / "db/agents-ide.db")))
with Session(engine) as session:
    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    run = session.get(Run, sys.argv[2])
    assert run is not None
    for index in range(650):
        append_event(
            session,
            run.id,
            "agent.message_delta",
            {"text": f"message-{index} <b data-untrusted='yes'>text</b>"},
            node_id="check",
        )
    append_event(
        session, run.id, "agent.tool_call", {"name": "read", "status": "completed"}, node_id="check"
    )
    record_artifact(session, run.id, ArtifactPayload("diff", body={"diff": "-old\n+new\n" * 6000}))
    session.execute(delete(RunEvent).where(RunEvent.run_id == run.id, RunEvent.sequence < 6))
    session.commit()
engine.dispose()
