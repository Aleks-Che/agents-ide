"""Current run configuration and the immutable initial execution snapshot."""

import json
from typing import Any

from agents_ide.persistence.models import Run


def effective_snapshot(run: Run) -> dict[str, Any]:
    snapshot: dict[str, Any] = json.loads(run.snapshot_json)
    snapshot.update(json.loads(run.runtime_json).get("template_configuration", {}))
    return snapshot
