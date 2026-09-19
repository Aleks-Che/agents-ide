"""Current run configuration and the immutable initial execution snapshot."""

import json
from typing import Any

from agents_ide.persistence.models import HarnessProfile, Run


def effective_snapshot(run: Run) -> dict[str, Any]:
    snapshot: dict[str, Any] = json.loads(run.snapshot_json)
    runtime = json.loads(run.runtime_json)
    snapshot.update(runtime.get("template_configuration", {}))
    if dependencies := runtime.get("group_dependencies"):
        snapshot["dependencies"] = dependencies
    return snapshot


def harness_configuration_matches(profile: HarnessProfile, pinned: dict[str, Any]) -> bool:
    """Catalog refreshes and probe results change version, not execution settings.

    Credentials and executable/server fingerprints are checked separately at dispatch.
    Missing configuration in an older snapshot is not evidence of compatibility.
    """
    return (
        {"harness_kind", "executable_path", "settings"} <= pinned.keys()
        and profile.harness_kind == pinned["harness_kind"]
        and profile.executable_path == pinned["executable_path"]
        and json.loads(profile.settings_json) == pinned["settings"]
    )
