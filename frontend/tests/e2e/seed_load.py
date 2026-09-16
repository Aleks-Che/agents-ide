"""100k-event UI fixture confined to a disposable Playwright database."""

import json
import sys
import time
from pathlib import Path

from sqlalchemy import insert
from sqlalchemy.orm import Session

from agents_ide.config import Settings
from agents_ide.engine.events import append_event
from agents_ide.persistence.database import create_database
from agents_ide.persistence.models import Run, RunEvent

directory = Path(sys.argv[1]).resolve()
local = Path(__file__).resolve().parents[3] / ".local"
assert directory.is_relative_to(local.resolve()) and directory.name.startswith("e2e-")
engine = create_database(Settings(data_dir=directory))
try:
    if sys.argv[3].startswith("tick-"):
        with Session(engine) as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            event = append_event(session, sys.argv[2], "command.finished", {"text": sys.argv[3]})
            persisted_at = event.persisted_at
            session.commit()
            print(persisted_at * 1000)
    else:
        now = time.time()
        for start in range(2, 100002, 2000):
            with Session(engine) as session:
                session.execute(
                    insert(RunEvent),
                    [
                        {
                            "id": f"{sys.argv[2][:12]}-{i}",
                            "run_id": sys.argv[2],
                            "sequence": i,
                            "event_version": 1,
                            "type": "agent.message_delta",
                            "occurred_at": now,
                            "persisted_at": now,
                            "worker_generation": 1,
                            "payload_json": json.dumps({"text": f"load-{i}"}),
                        }
                        for i in range(start, start + 2000)
                    ],
                )
                session.commit()
        with Session(engine) as session:
            session.get(Run, sys.argv[2]).detailed_event_count = 100000
            session.commit()
finally:
    engine.dispose()
