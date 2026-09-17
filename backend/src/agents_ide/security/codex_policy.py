"""Codex 0.153.4 named read profile for isolated Council artifacts.

See config/src/permissions_toml.rs at rust-v0.153.4. Legacy sandboxPolicy
must not override this profile on turn/start. Unknown versions fail closed.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

from agents_ide.errors import AppError

PROFILE = "agents_ide_council"
SUPPORTED_VERSION = "codex-cli 0.153.4"


def config_for(root: Path) -> dict[str, Any]:
    if sys.platform != "win32":
        raise AppError(
            "council_harness_real_unverified",
            "Council Codex requires verified Windows sandbox",
            422,
        )
    return {
        "default_permissions": PROFILE,
        "permissions": {
            PROFILE: {
                "filesystem": {str(root): "read", os.environ["SYSTEMROOT"]: "read"},
                "network": {"enabled": False},
            }
        },
        "web_search": "disabled",
        "notify": [],
        "project_doc_max_bytes": 0,
        "include_apps_instructions": False,
        "allow_login_shell": False,
        "features": {
            name: False
            for name in (
                "apps",
                "connectors",
                "enable_mcp_apps",
                "plugins",
                "remote_plugin",
                "plugin_hooks",
                "hooks",
                "codex_hooks",
                "browser_use",
                "browser_use_external",
                "computer_use",
                "in_app_browser",
                "js_repl",
                "code_mode",
                "code_mode_host",
                "collab",
                "multi_agent",
                "multi_agent_mode",
                "multi_agent_v2",
                "enable_fanout",
                "image_generation",
                "view_image",
                "tool_search",
                "tool_suggest",
                "skill_search",
                "memories",
                "memory_tool",
                "external_agent_memory_import",
                "shell_snapshot",
                "workspace_dependencies",
                "in_app_local_automation",
                "remote_control",
            )
        }
        | {"skip_host_skill_discovery": True},
    }


def _toml(value: Any) -> str:
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k) + "=" + _toml(v) for k, v in value.items()) + "}"
    return json.dumps(value, ensure_ascii=False)


def arguments(config: dict[str, Any]) -> list[str]:
    return [arg for key, value in config.items() for arg in ("-c", key + "=" + _toml(value))]
