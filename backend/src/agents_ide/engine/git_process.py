"""Bounded Git transport. Every process (including hooks) belongs to one Job."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents_ide.errors import AppError
from agents_ide.security.workspace_read import hold_command_directory


class GitError(AppError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(code, message, 409, details)


def git_env() -> dict[str, str]:
    from agents_ide.engine.commands import command_environment

    env = command_environment({})
    env.update(
        GIT_OPTIONAL_LOCKS="0",
        GIT_TERMINAL_PROMPT="0",
        GIT_EDITOR="true",
        GIT_AUTHOR_NAME="Agents IDE",
        GIT_AUTHOR_EMAIL="agents-ide@localhost",
        GIT_COMMITTER_NAME="Agents IDE",
        GIT_COMMITTER_EMAIL="agents-ide@localhost",
        LC_ALL="C.UTF-8",
        LANG="C.UTF-8",
    )
    # Git needs the user's config/search root, but never inherits tokens or GIT_* overrides.
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


@dataclass
class GitTransport:
    launcher: Callable[..., Any] | None = None
    stop: threading.Event | None = None
    deadline: float | None = None
    executable: str | None = None
    on_finished: Callable[[], None] | None = None

    def run(
        self,
        workspace: Path,
        args: list[str],
        *,
        data: bytes = b"",
        env: dict[str, str] | None = None,
        timeout: float = 30,
    ) -> subprocess.CompletedProcess[bytes]:
        from agents_ide.worker.processes import ProcessGroup

        executable = self.executable or shutil.which("git")
        if not executable:
            raise GitError("git_unavailable", "Git is unavailable")
        deadline = min(self.deadline or float("inf"), time.monotonic() + timeout)
        if (self.stop and self.stop.is_set()) or time.monotonic() >= deadline:
            raise GitError("git_interrupted", "Git dispatch interrupted")
        argv = [
            executable,
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.quotePath=false",
            "-C",
            str(workspace),
            *args,
        ]
        environment = {**git_env(), **(env or {})}
        with hold_command_directory(workspace, workspace):
            if self.launcher:
                started = self.launcher(argv, workspace, environment)
                child, group = started.popen, started.group
            else:
                group = ProcessGroup()
                try:
                    child = group.popen_stdio(argv, workspace, environment)
                except BaseException:
                    group.close()
                    raise
        outputs = [bytearray(), bytearray()]
        cap, total, truncated = 16 * 1024 * 1024, 0, False
        lock = threading.Lock()

        def drain(stream: Any, destination: bytearray) -> None:
            nonlocal total, truncated
            try:
                while chunk := stream.buffer.read(65536):
                    with lock:
                        count = min(len(chunk), cap - total)
                        destination.extend(chunk[:count])
                        total += count
                        truncated |= count < len(chunk)
            except (OSError, ValueError):
                pass

        def feed() -> None:
            try:
                if child.stdin:
                    child.stdin.buffer.write(data)
                    child.stdin.close()
            except (OSError, ValueError):
                pass

        threads = [
            threading.Thread(target=drain, args=(child.stdout, outputs[0]), daemon=True),
            threading.Thread(target=drain, args=(child.stderr, outputs[1]), daemon=True),
            threading.Thread(target=feed, daemon=True),
        ]
        for thread in threads:
            thread.start()
        interrupted = False
        try:
            while child.poll() is None:
                if (self.stop and self.stop.is_set()) or time.monotonic() >= deadline:
                    interrupted = True
                    break
                time.sleep(0.02)
        finally:
            # A hook may leave children behind even when the Git root exits.
            if self.launcher and started.entry:
                from agents_ide.worker.processes import capture_tree

                capture_tree(started.entry)
            group.close()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                raise GitError("process_not_responding", "Git process tree did not stop") from None
            for thread in threads:
                thread.join(timeout=5)
            if any(thread.is_alive() for thread in threads):
                raise GitError("process_not_responding", "Git streams did not close")
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream:
                    stream.close()
            if self.on_finished:
                self.on_finished()
        if interrupted:
            raise GitError("git_timeout", "Git operation was interrupted or timed out")
        if truncated:
            raise GitError("git_output_limit", "Git output exceeded its limit")
        return subprocess.CompletedProcess(
            argv, child.returncode, bytes(outputs[0]), bytes(outputs[1])
        )


_TRANSPORT: ContextVar[GitTransport | None] = ContextVar("git_transport", default=None)


@contextmanager
def using_transport(transport: GitTransport) -> Iterator[None]:
    token = _TRANSPORT.set(transport)
    try:
        yield
    finally:
        _TRANSPORT.reset(token)


def run_git(
    workspace: Path,
    args: list[str],
    *,
    data: bytes = b"",
    env: dict[str, str] | None = None,
    timeout: float = 30,
) -> bytes:
    result = (_TRANSPORT.get() or GitTransport()).run(
        workspace, args, data=data, env=env, timeout=timeout
    )
    if result.returncode:
        raise GitError(
            "git_failed",
            "Git command failed",
            details={
                "command": args[0],
                "exit_code": result.returncode,
                "stderr": result.stderr.decode("utf-8", "replace")[:8192],
            },
        )
    return result.stdout


def optional_git(workspace: Path, args: list[str]) -> str | None:
    result = (_TRANSPORT.get() or GitTransport()).run(workspace, args)
    return result.stdout.decode("utf-8", "strict").strip() if result.returncode == 0 else None
