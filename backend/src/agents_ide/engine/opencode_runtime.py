"""Owned OpenCode lifecycle; credentials are memory-only and never reattached.

Probes use a temporary workspace and ProcessGroup. Runs use the same launch
policy via ProcessSupervisor (registration and fencing before process resume).
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import socket
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil

from agents_ide.adapters.opencode import (
    RunSession,
    fetch_opencode_version,
    list_opencode_models,
    probe_json,
)
from agents_ide.engine.commands import command_environment
from agents_ide.errors import AppError
from agents_ide.worker.processes import ProcessGroup, process_state

if TYPE_CHECKING:
    from agents_ide.worker.processes import ProcessRegistryEntry, ProcessSupervisor


def free_loopback_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def validate_settings(settings: dict[str, Any], *, execution: bool = True) -> None:
    if "auto_approve" in settings and type(settings["auto_approve"]) is not bool:
        raise AppError("configuration_invalid", "Некорректный режим подтверждения OpenCode", 409)
    if settings.get("auth", True) is not True or settings.get("serve_args") or settings.get("env"):
        raise AppError(
            "configuration_invalid",
            "OpenCode auth, arguments and environment are server-managed",
            409,
        )
    if execution and settings.get("permission_mode") not in {"no_tools", "native"}:
        raise AppError(
            "configuration_invalid",
            "Выберите режим OpenCode: native или no_tools",
            409,
        )


def server_environment(
    password: str, username: str = "opencode", *, permission_mode: str = "no_tools"
) -> dict[str, str]:
    env = command_environment()
    # Native harness credentials may be read from its own store; never inherit
    # provider tokens, proxy settings, NODE_OPTIONS, BUN_OPTIONS or config injection.
    for key in (
        "APPDATA",
        "LOCALAPPDATA",
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        if key in os.environ:
            env[key] = os.environ[key]
    env.update(
        OPENCODE_SERVER_USERNAME=username,
        OPENCODE_SERVER_PASSWORD=password,
        OPENCODE_DISABLE_AUTOUPDATE="true",
        OPENCODE_DISABLE_SHARE="true",
        OPENCODE_DISABLE_PROJECT_CONFIG="true",
        OPENCODE_DISABLE_CLAUDE_CODE="true",
        OPENCODE_DISABLE_LSP_DOWNLOAD="true",
        OPENCODE_CONFIG_CONTENT=json.dumps(
            {
                "permission": {"*": "deny"},
                "autoupdate": False,
                "share": "disabled",
                "mcp": {},
                "lsp": False,
                "formatter": False,
            }
        ),
    )
    if permission_mode == "native":
        # Keep the user's native tools and allow/ask/deny policy, including project
        # configuration. Do not replace it with an allow-all permission override.
        for key in ("OPENCODE_DISABLE_PROJECT_CONFIG", "OPENCODE_DISABLE_CLAUDE_CODE"):
            env.pop(key, None)
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps({"autoupdate": False, "share": "disabled"})
    return env


@dataclass
class OpenCodeRuntime:
    base_url: str
    username: str
    password: str = field(repr=False)
    workspace_path: Path
    group: ProcessGroup = field(repr=False)
    pid: int
    create_time: float
    server_version: str | None = None
    cached_models: tuple[str, ...] = ()
    entry: ProcessRegistryEntry | None = field(default=None, repr=False)
    supervisor: ProcessSupervisor | None = field(default=None, repr=False)
    permission_mode: str = "no_tools"
    _closed: bool = False

    @classmethod
    def start(
        cls,
        *,
        executable: str,
        workspace_path: Path,
        supervisor: ProcessSupervisor | None = None,
        start_timeout: float = 30,
        attempt_id: str | None = None,
        check_owned: Callable[[], None] | None = None,
        stop_event: threading.Event | None = None,
        port: int | None = None,
        permission_mode: str = "no_tools",
    ) -> OpenCodeRuntime:
        validate_settings({"permission_mode": permission_mode})
        path = Path(executable)
        if (
            not path.is_absolute()
            or not path.is_file()
            or path.suffix.lower() in {".ps1", ".cmd", ".bat"}
        ):
            raise AppError(
                "configuration_invalid", "Use the absolute native OpenCode executable path", 409
            )
        deadline = time.monotonic() + start_timeout
        last_error: AppError | None = None
        for _ in range(3):
            if check_owned:
                check_owned()
            if stop_event and stop_event.is_set():
                raise AppError("dispatch_blocked", "OpenCode start interrupted", 409)
            chosen_port = port or free_loopback_port()
            password = secrets.token_urlsafe(32)
            argv = [
                str(path),
                "serve",
                "--hostname",
                "127.0.0.1",
                "--port",
                str(chosen_port),
            ]
            if permission_mode == "no_tools":
                argv.append("--pure")
            group = ProcessGroup()
            runtime = None
            entry = None
            try:
                if supervisor:
                    group.close()
                    entry = supervisor.start(
                        argv,
                        cwd=workspace_path,
                        env=server_environment(password, permission_mode=permission_mode),
                        role="opencode_server",
                        kind="harness",
                        attempt_id=attempt_id,
                        transport="http",
                        port=chosen_port,
                    )
                    assert entry.group is not None
                    group, pid, created = entry.group, entry.pid, entry.create_time
                else:
                    child = group.start(
                        argv,
                        workspace_path,
                        server_environment(password, permission_mode=permission_mode),
                    )
                    pid = child.pid
                    created = psutil.Process(pid).create_time()
                runtime = cls(
                    f"http://127.0.0.1:{chosen_port}",
                    "opencode",
                    password,
                    workspace_path,
                    group,
                    pid,
                    created,
                    entry=entry,
                    supervisor=supervisor,
                    permission_mode=permission_mode,
                )
                while time.monotonic() < deadline:
                    if check_owned:
                        check_owned()
                    if stop_event and stop_event.is_set():
                        raise AppError("dispatch_blocked", "OpenCode start interrupted", 409)
                    if process_state(pid, created) != "alive":
                        raise AppError(
                            "opencode_server_exited",
                            "OpenCode exited before health confirmation",
                            409,
                        )
                    version = fetch_opencode_version(
                        runtime.base_url,
                        password=password,
                        timeout_seconds=min(0.5, max(0.1, deadline - time.monotonic())),
                    )
                    if version:
                        # Auth health alone cannot prove this is our listener. Check its
                        # owner and directory before giving it context or provider secrets.
                        listeners = [
                            c
                            for member in group.members()
                            for c in member.net_connections(kind="tcp")
                        ]
                        if not any(
                            c.status == psutil.CONN_LISTEN
                            and c.laddr.port == chosen_port
                            and c.laddr.ip == "127.0.0.1"
                            for c in listeners
                        ):
                            raise AppError(
                                "port_unavailable",
                                "OpenCode listener ownership is not confirmed",
                                409,
                            )
                        info = probe_json(
                            runtime.base_url,
                            "/path",
                            password=password,
                            directory=str(workspace_path),
                        )
                        if (
                            not isinstance(info, dict)
                            or Path(info.get("directory", "")).resolve() != workspace_path.resolve()
                        ):
                            raise AppError(
                                "configuration_invalid", "OpenCode directory mismatch", 409
                            )
                        runtime.server_version = version
                        runtime.cached_models = list_opencode_models(
                            runtime.base_url, password=password, directory=str(workspace_path)
                        )
                        return runtime
                    (stop_event or threading.Event()).wait(0.1)
                raise AppError("opencode_startup_timeout", "OpenCode health deadline expired", 409)
            except AppError as exc:
                last_error = exc
                if runtime:
                    runtime.close()
                else:
                    group.close()
                if (
                    exc.code not in {"port_unavailable", "opencode_server_exited"}
                    or port
                    or time.monotonic() >= deadline
                ):
                    raise
            except BaseException:
                if runtime:
                    runtime.close()
                else:
                    group.close()
                raise
        raise last_error or AppError("port_unavailable", "No owned OpenCode port available", 409)

    def session(self) -> RunSession:
        return RunSession(
            self.base_url,
            self.username,
            self.password,
            str(self.workspace_path),
            {
                "permission_mode": self.permission_mode,
                "model_metadata": getattr(self.cached_models, "metadata", {}),
            },
            self.server_version,
        )

    def close(self) -> None:
        if self._closed:
            return
        if self.entry is not None:
            from agents_ide.worker.processes import capture_tree

            capture_tree(self.entry)
        self.group.close()
        if self.entry is not None:
            from agents_ide.worker.processes import wait_descendants_stopped

            if not wait_descendants_stopped(self.entry, 5):
                raise AppError(
                    "process_not_responding", "OpenCode tree termination is unconfirmed", 409
                )
            if self.supervisor:
                self.supervisor.refresh_health()
        else:
            with contextlib.suppress(psutil.NoSuchProcess):
                psutil.Process(self.pid).wait(timeout=5)
        self._closed = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "pid": self.pid,
            "create_time": self.create_time,
            "server_version": self.server_version,
            "workspace_path": str(self.workspace_path),
            "catalog_count": len(self.cached_models),
            "permission_mode": self.permission_mode,
        }


def probe_executable(
    executable: str, settings: dict[str, Any]
) -> tuple[str | None, tuple[str, ...]]:
    validate_settings(settings, execution=False)
    with tempfile.TemporaryDirectory(prefix="agents-ide-opencode-probe-") as directory:
        runtime = OpenCodeRuntime.start(executable=executable, workspace_path=Path(directory))
        try:
            return runtime.server_version, runtime.cached_models
        finally:
            runtime.close()
