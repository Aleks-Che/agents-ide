"""Execute only the requested simulated Run in the disposable Playwright database."""

import json
import sys
from pathlib import Path

from sqlalchemy import delete, update
from sqlalchemy.orm import sessionmaker

from agents_ide.config import Settings
from agents_ide.engine.queue import claim_next_job, release_job
from agents_ide.engine.runner import Runner
from agents_ide.persistence.database import create_database
from agents_ide.persistence.models import QueueJob, Run, RunEvent

directory = Path(sys.argv[1]).resolve()
local = Path(__file__).resolve().parents[3] / ".local"
assert directory.is_relative_to(local.resolve()) and directory.name.startswith("e2e-")
settings = Settings(data_dir=directory)
engine = create_database(settings)
factory = sessionmaker(engine, expire_on_commit=False)
try:
    with factory() as session:
        row = session.get(Run, sys.argv[2])
        assert row and json.loads(row.snapshot_json)["execution_mode"] == "simulated"
    if sys.argv[3] == "execute":
        # Other e2e cases can leave queued Runs: prioritize this job only.
        with factory() as session:
            session.execute(
                update(QueueJob).where(QueueJob.run_id == row.id).values(available_at=0)
            )
            session.commit()
        job = claim_next_job(factory, worker_id="selection-browser", lease_seconds=30)
        assert job and job.run_id == row.id
        result = Runner(
            session_factory=factory,
            worker_id="selection-browser",
            generation=job.generation,
            data_dir=directory,
            secret_store=None,
        ).execute(row.id)
        release_job(
            factory,
            job_id=job.job_id,
            worker_id="selection-browser",
            expected_generation=job.generation,
        )
        print(result.final_state.value)
    elif sys.argv[3] == "prune":
        with factory() as session:
            session.execute(delete(RunEvent).where(RunEvent.run_id == row.id))
            session.commit()
    else:
        raise ValueError("Unknown fixture action")
finally:
    engine.dispose()
