"""One owned Codex App Server per active Run, including startup and catalog."""

from __future__ import annotations

import contextlib
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agents_ide.adapters.codex import CodexAdapter, CodexStream, RunSession
from agents_ide.engine.commands import command_environment
from agents_ide.errors import AppError

if TYPE_CHECKING:
    from agents_ide.worker.processes import ProcessRegistryEntry, ProcessSupervisor

STARTUP_TIMEOUT_SECONDS = 30.0


def validate_settings(settings: dict[str, Any], *, execution: bool = True) -> None:
    if settings.get("serve_args") or settings.get("env"):
        raise AppError(
            "configuration_invalid", "Codex arguments and environment are server-managed", 409
        )
    if settings.get("approval_policy", "never") != "never":
        raise AppError("configuration_invalid", "Codex requires approval_policy=never", 409)
    if execution and settings.get("permission_mode") != "read_only":
        raise AppError(
            "configuration_invalid",
            "Codex requires permission_mode=read_only until write isolation is verified",
            409,
        )


def codex_environment() -> dict[str, str]:
    env = command_environment()
    for key in (
        "CODEX_HOME",
        "USERPROFILE",
        "HOME",
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def fetch_codex_version(executable: str, *, timeout_seconds: float = 5.0) -> str | None:
    try:
        result = subprocess.run(
            [executable, "--version"],
            env=codex_environment(),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in (result.stdout or "").splitlines():
        if line.startswith(("codex-cli ", "codex ")):
            return line.strip()
    return None


@dataclass
class CodexRuntime:
    workspace_path: Path
    executable: str
    stream: CodexStream
    server_version: str | None = None
    cached_models: tuple[str, ...] = ()
    entry: ProcessRegistryEntry | None = None
    supervisor: ProcessSupervisor | None = None
    _closed: bool = False

    @classmethod
    def start(
        cls,
        *,
        executable: str,
        workspace_path: Path,
        supervisor: ProcessSupervisor | None = None,
        start_timeout: float = STARTUP_TIMEOUT_SECONDS,
        attempt_id: str | None = None,
        check_owned: Callable[[], None] | None = None,
        stop_event: threading.Event | None = None,
    ) -> CodexRuntime:
        path = Path(executable)
        if (
            not path.is_absolute()
            or not path.is_file()
            or path.suffix.lower() in {".ps1", ".cmd", ".bat"}
        ):
            raise AppError(
                "configuration_invalid", "Use the absolute native Codex executable path", 409
            )
        deadline = time.monotonic() + start_timeout

        def check() -> None:
            if check_owned:
                check_owned()
            if stop_event and stop_event.is_set():
                raise AppError("dispatch_blocked", "Codex startup interrupted", 409)
            if time.monotonic() >= deadline:
                raise AppError("codex_startup_timeout", "Codex startup deadline expired", 409)

        check()
        argv = [str(path), "app-server", "--listen", "stdio://"]
        env = codex_environment()
        entry = None
        if supervisor is None:
            stream = CodexStream.open(argv, str(workspace_path), env)
        else:
            entry, process = supervisor.start_stdio(
                argv,
                workspace_path,
                env,
                role="codex_app_server",
                kind="harness",
                attempt_id=attempt_id,
                stdin=subprocess.PIPE,
            )
            try:
                stream = CodexStream.from_process(process)
                stream.group = entry.group
            except BaseException:
                if entry.group:
                    entry.group.close()
                raise
        runtime = cls(workspace_path, str(path), stream, entry=entry, supervisor=supervisor)
        try:
            adapter = CodexAdapter(
                stream=stream, session=runtime.session(), request_timeout=start_timeout
            )
            adapter.initialize(check=check)
            runtime.cached_models = adapter.list_models(check=check)
            check()
            runtime.server_version = fetch_codex_version(
                str(path), timeout_seconds=min(2.0, max(0.1, deadline - time.monotonic()))
            )
            check()
            return runtime
        except BaseException:
            runtime.close()
            raise

    def session(self) -> RunSession:
        return RunSession(
            str(self.workspace_path), {"permission_mode": "read_only"}, self.server_version
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.entry is not None:
            from agents_ide.worker.processes import capture_tree

            with contextlib.suppress(Exception):
                capture_tree(self.entry)  # Capture descendants before terminating their parent.
        self.stream.close()
        if self.supervisor is not None:
            self.supervisor.refresh_health()

    def to_dict(self) -> dict[str, Any]:
        return {
            "executable": self.executable,
            "pid": self.stream.process.pid,
            "create_time": self.entry.create_time if self.entry else None,
            "server_version": self.server_version,
            "workspace_path": str(self.workspace_path),
            "catalog_count": len(self.cached_models),
            "permission_mode": "read_only",
        }


def probe_executable(
    executable: str, workspace_path: Path, settings: dict[str, Any]
) -> tuple[str | None, tuple[str, ...]]:
    validate_settings(settings, execution=False)
    runtime = CodexRuntime.start(executable=executable, workspace_path=workspace_path)
    try:
        return runtime.server_version, runtime.cached_models
    finally:
        runtime.close()
