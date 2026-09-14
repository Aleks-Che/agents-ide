"""Owned process groups. Windows processes enter a Job before they run."""

import contextlib
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import psutil


class ProcessGroup:
    def __init__(self) -> None:
        self.job: Any = None
        self.handles: list[Any] = []
        self.children: list[subprocess.Popen[Any]] = []
        if sys.platform == "win32":
            import win32job

            # pywin32's stub incorrectly declares this API as returning None.
            create_job = cast(Callable[..., Any], win32job.CreateJobObject)
            self.job = create_job(None, "")
            limits = win32job.QueryInformationJobObject(
                self.job, win32job.JobObjectExtendedLimitInformation
            )
            limits["BasicLimitInformation"]["LimitFlags"] = (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            win32job.SetInformationJobObject(
                self.job, win32job.JobObjectExtendedLimitInformation, limits
            )

    def popen_stdio(
        self, argv: list[str], cwd: Path, env: dict[str, str] | None = None
    ) -> subprocess.Popen[str]:
        """Stdio transport with the same suspended-start ownership invariant."""
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NO_WINDOW | 0x00000004  # CREATE_SUSPENDED
        child = subprocess.Popen(
            argv,
            env=env,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            creationflags=flags,
            start_new_session=sys.platform != "win32",
        )
        if sys.platform == "win32":
            import win32api
            import win32con
            import win32job
            import win32process

            handle = win32api.OpenProcess(
                win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, child.pid
            )
            try:
                win32job.AssignProcessToJobObject(self.job, handle)
                primary = psutil.Process(child.pid).threads()[0].id
                thread = win32api.OpenThread(win32con.THREAD_SUSPEND_RESUME, False, primary)
                try:
                    win32process.ResumeThread(thread)
                finally:
                    win32api.CloseHandle(thread)
            except BaseException:
                child.kill()
                child.wait(timeout=5)
                raise
            finally:
                win32api.CloseHandle(handle)
        else:
            self.children.append(child)
        return child

    def start(self, argv: list[str], cwd: Path, env: dict[str, str]) -> psutil.Process:
        if sys.platform == "win32":
            import win32api
            import win32con
            import win32job
            import win32process

            handle, thread, pid, _ = win32process.CreateProcess(
                argv[0],
                subprocess.list2cmdline(argv),
                None,
                None,
                False,
                win32con.CREATE_SUSPENDED
                | win32con.CREATE_NO_WINDOW
                | win32con.CREATE_UNICODE_ENVIRONMENT,
                env,
                str(cwd),
                win32process.STARTUPINFO(),
            )
            try:
                win32job.AssignProcessToJobObject(self.job, handle)
                process = psutil.Process(pid)
                win32process.ResumeThread(thread)
            except BaseException:
                win32process.TerminateProcess(handle, 1)
                win32api.CloseHandle(handle)
                raise
            finally:
                win32api.CloseHandle(thread)
            self.handles.append(handle)
            return process
        child = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.children.append(child)
        return psutil.Process(child.pid)

    def close(self) -> None:
        if self.job is not None:
            self.job.Close()
            self.job = None
            for handle in self.handles:
                handle.Close()
            self.handles.clear()
        if sys.platform == "win32":
            return
        for child in self.children:
            import signal

            try:
                os.killpg(child.pid, signal.SIGTERM)
                child.wait(timeout=3)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=3)
        self.children.clear()


def is_running(process: psutil.Process) -> bool:
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def wait_stopped(processes: list[psutil.Process], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(is_running(process) for process in processes):
            return True
        time.sleep(0.1)
    return not any(is_running(process) for process in processes)
