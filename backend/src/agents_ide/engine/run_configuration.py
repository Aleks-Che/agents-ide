"""Current run configuration and the immutable initial execution snapshot."""

import hashlib
import json
from typing import Any

from agents_ide.domain.common import to_json
from agents_ide.persistence.models import HarnessProfile, Run


def commit_connection_source(snapshot: dict[str, Any], node_id: str) -> str:
    generation = snapshot["dependencies"]["nodes"][node_id]["message_generation"]
    provider = snapshot["dependencies"]["provider_connections"].get(generation["connection_id"])
    return hashlib.sha256(to_json([generation, provider]).encode()).hexdigest()


def commit_message_configuration(
    snapshot: dict[str, Any], runtime: dict[str, Any], node_id: str
) -> dict[str, Any] | None:
    config = snapshot["dependencies"]["nodes"][node_id]
    if not config.get("generate_message"):
        return None
    generation = dict(config["message_generation"])
    accepted = runtime.get("commit_connection_versions", {}).get(node_id, {})
    if accepted.get("source_hash") == commit_connection_source(snapshot, node_id):
        generation["resource_version"] = accepted["resource_version"]
    return generation


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
