"""Run only the named browser-test Council against loopback HTTP."""

import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from agents_ide.config import Settings
from agents_ide.engine.planning_worker import dispatch_planning_job
from agents_ide.persistence.database import create_database
from agents_ide.persistence.models import PlanningMember
from agents_ide.security.secrets import SecretStore

settings = Settings(data_dir=Path(sys.argv[1]))
engine = create_database(settings)
factory = sessionmaker(engine, expire_on_commit=False)
try:
    with factory() as session:
        members = list(session.scalars(select(PlanningMember).where(PlanningMember.job_id == sys.argv[2])))
        assert members
        for member in members:
            for candidate in json.loads(member.candidates_json):
                url = urlsplit(candidate["connection"]["base_url"])
                assert url.scheme == "http" and url.hostname == "127.0.0.1"
    result = dispatch_planning_job(factory, sys.argv[2], secret_store=SecretStore(settings.data_dir / "secrets"))
    print(result.final_state.value)
finally:
    engine.dispose()
