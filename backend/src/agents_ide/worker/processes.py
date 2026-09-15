"""Owned process groups. Windows processes enter a Job before they run.

Stage 5 extends the supervisor with interrupt/kill deadlines and a PID
registry. The supervisor never kills processes it does not own; foreign
PIDs and generations are returned untouched. The registry records
owner generation, started/create time, parent PID, executable and the
last external event so recovery and health checks can prove what is
still alive.
"""

import contextlib
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import psutil
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from agents_ide.domain.common import new_id, to_json, utc_now
from agents_ide.engine.queue import owned_job
from agents_ide.errors import AppError
from agents_ide.persistence.models import ProcessSupervision, Run
from agents_ide.services.transactions import begin_write


class ProcessGroup:
    def __init__(self) -> None:
        self._handle_lock = threading.RLock()
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
        self,
        argv: list[str],
        cwd: Path,
        env: dict[str, str] | None = None,
        before_resume: Callable[[psutil.Process], None] | None = None,
        *,
        stdin: int = subprocess.PIPE,
        stderr: int = subprocess.PIPE,
    ) -> subprocess.Popen[str]:
        """Stdio transport with the same suspended-start ownership invariant."""
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NO_WINDOW | 0x00000004  # CREATE_SUSPENDED
        command = argv
        if sys.platform != "win32" and before_resume:
            command = [
                sys.executable,
                "-c",
                "import os,signal,sys; os.kill(os.getpid(),signal.SIGSTOP); "
                "os.execvpe(sys.argv[1],sys.argv[1:],os.environ)",
                *argv,
            ]
        child = subprocess.Popen(
            command,
            env=env,
            cwd=cwd,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            encoding="utf-8",
            errors="replace",
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
                if before_resume:
                    before_resume(psutil.Process(child.pid))
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
            if before_resume:
                import signal

                process = psutil.Process(child.pid)
                try:
                    deadline = time.monotonic() + 5
                    while process.status() != psutil.STATUS_STOPPED:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Child did not stop before registration")
                        time.sleep(0.01)
                    before_resume(process)
                    os.kill(child.pid, signal.SIGCONT)
                except BaseException:
                    child.kill()
                    child.wait(timeout=5)
                    raise
        return child

    def start(
        self,
        argv: list[str],
        cwd: Path,
        env: dict[str, str],
        before_resume: Callable[[psutil.Process], None] | None = None,
    ) -> psutil.Process:
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
                if before_resume:
                    before_resume(process)
                win32process.ResumeThread(thread)
            except BaseException:
                win32process.TerminateProcess(handle, 1)
                win32api.CloseHandle(handle)
                raise
            finally:
                win32api.CloseHandle(thread)
            self.handles.append(handle)
            return process
        command = argv
        if before_resume:
            command = [
                sys.executable,
                "-c",
                "import os,signal,sys; os.kill(os.getpid(),signal.SIGSTOP); "
                "os.execvpe(sys.argv[1],sys.argv[1:],os.environ)",
                *argv,
            ]
        child = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.children.append(child)
        process = psutil.Process(child.pid)
        if before_resume:
            import signal

            try:
                deadline = time.monotonic() + 5
                while process.status() != psutil.STATUS_STOPPED:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Child did not stop before registration")
                    time.sleep(0.01)
                before_resume(process)
                os.kill(child.pid, signal.SIGCONT)
            except BaseException:
                child.kill()
                child.wait(timeout=5)
                raise
        return process

    def members(self) -> list[psutil.Process]:
        with self._handle_lock:
            return self._members()

    def _members(self) -> list[psutil.Process]:
        if self.job is not None:
            import win32job

            info = win32job.QueryInformationJobObject(
                self.job, win32job.JobObjectBasicProcessIdList
            )
            pids = info if isinstance(info, (list, tuple)) else info.get("ProcessIdList", [])
        else:
            pids = [p.pid for p in self.children]
        members = []
        for pid in pids:
            with contextlib.suppress(psutil.NoSuchProcess):
                process = psutil.Process(pid)
                members.append(process)
                members.extend(process.children(recursive=True))
        return members

    def close(self) -> None:
        with self._handle_lock:
            self._close()

    def _close(self) -> None:
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


@dataclass
class ProcessRegistryEntry:
    pid: int
    started_at: float
    create_time: float
    parent_pid: int | None
    executable: str | None
    kind: str
    role: str
    owner_generation: int
    run_id: str
    step_attempt_id: str | None
    state: str = "started"
    interrupt_requested_at: float | None = None
    killed_at: float | None = None
    last_external_event_at: float | None = None
    finished_at: float | None = None
    process: psutil.Process | None = field(default=None, repr=False)
    descendants: dict[int, float] = field(default_factory=dict)
    group: ProcessGroup | None = field(default=None, repr=False)
    interrupt_callback: Callable[[Any], bool] | None = field(default=None, repr=False)


class ProcessRegistry:
    """In-memory PID registry owned by the worker.

    Stale entries are pruned only by ``generation``. A process from a
    previous generation can never be signalled by the current worker.
    Foreign PIDs are matched against ``owner_generation`` before any
    action; supervisors only stop their own descendants.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[int, int], ProcessRegistryEntry] = {}
        self.lock = threading.RLock()
        self.aborts: dict[str, threading.Event] = {}

    def abort_all(self) -> None:
        with self.lock:
            for event in self.aborts.values():
                event.set()

    def entries(self) -> list[ProcessRegistryEntry]:
        with self.lock:
            return list(self._entries.values())

    def register(
        self,
        *,
        process: psutil.Process,
        kind: str,
        role: str,
        owner_generation: int,
        run_id: str,
        step_attempt_id: str | None,
    ) -> ProcessRegistryEntry:
        create_time = float(process.create_time())
        try:
            parent_pid = process.ppid()
        except (psutil.Error, OSError):
            parent_pid = None
        try:
            executable = process.exe()
        except (psutil.Error, OSError):
            executable = None
        entry = ProcessRegistryEntry(
            pid=process.pid,
            started_at=time.time(),
            create_time=create_time,
            parent_pid=parent_pid,
            executable=executable,
            kind=kind,
            role=role,
            owner_generation=owner_generation,
            run_id=run_id,
            step_attempt_id=step_attempt_id,
            process=process,
        )
        with self.lock:
            self._entries[(process.pid, owner_generation)] = entry
        return entry

    def mark_external_event(
        self, pid: int, owner_generation: int, *, when: float | None = None
    ) -> None:
        entry = self._entries.get((pid, owner_generation))
        if entry is not None:
            entry.last_external_event_at = when if when is not None else time.time()

    def mark_interrupted(self, pid: int, owner_generation: int) -> None:
        entry = self._entries.get((pid, owner_generation))
        if entry is not None:
            entry.state = "interrupt_requested"
            entry.interrupt_requested_at = time.time()

    def mark_killed(self, pid: int, owner_generation: int) -> None:
        entry = self._entries.get((pid, owner_generation))
        if entry is not None:
            entry.state = "killed"
            entry.killed_at = time.time()

    def mark_finished(
        self, pid: int, owner_generation: int, *, create_time: float | None = None
    ) -> None:
        with self.lock:
            entry = self._entries.get((pid, owner_generation))
            if entry is not None:
                if create_time is not None and entry.create_time != create_time:
                    return  # A later process may already have reused this PID.
                entry.state = "finished"
                entry.finished_at = time.time()
            self._entries.pop((pid, owner_generation), None)

    def lookup(self, pid: int, owner_generation: int) -> ProcessRegistryEntry | None:
        return self._entries.get((pid, owner_generation))

    def by_run(self, run_id: str) -> list[ProcessRegistryEntry]:
        return [entry for entry in self.entries() if entry.run_id == run_id]

    def drop_run(self, run_id: str) -> list[ProcessRegistryEntry]:
        keys = [key for key, entry in self._entries.items() if entry.run_id == run_id]
        return [self._entries.pop(key) for key in keys]

    def active(self, owner_generation: int) -> list[ProcessRegistryEntry]:
        return [entry for entry in self.entries() if entry.owner_generation == owner_generation]

    def clear(self) -> None:
        self._entries.clear()


def process_state(pid: int, create_time: float) -> str:
    """An access failure is unknown, never proof that a writer stopped."""
    try:
        process = psutil.Process(pid)
        if abs(process.create_time() - create_time) > 0.000001:
            return "dead"
        return (
            "alive" if process.is_running() and process.status() != psutil.STATUS_ZOMBIE else "dead"
        )
    except psutil.NoSuchProcess:
        return "dead"
    except (psutil.Error, OSError, ValueError):
        return "unknown"


def is_alive_pid(pid: int, create_time: float) -> bool:
    return process_state(pid, create_time) != "dead"


def capture_tree(entry: ProcessRegistryEntry) -> bool:
    try:
        members = entry.group.members() if entry.group else []
        if process_state(entry.pid, entry.create_time) == "alive":
            members.extend(psutil.Process(entry.pid).children(recursive=True))
        for process in members:
            with contextlib.suppress(psutil.NoSuchProcess):
                entry.descendants[process.pid] = process.create_time()
        return True
    except (psutil.Error, OSError):
        return False


def wait_descendants_stopped(entry: ProcessRegistryEntry, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if not capture_tree(entry):
            return False
        identities = {entry.pid: entry.create_time, **entry.descendants}
        if all(process_state(pid, created) == "dead" for pid, created in identities.items()):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def interrupt(
    entry: ProcessRegistryEntry,
    *,
    interrupt_fn: Callable[[ProcessRegistryEntry], bool] | None = None,
    kill_fn: Callable[[ProcessRegistryEntry], bool] | None = None,
    cooperative_seconds: float = 10.0,
    kill_seconds: float = 5.0,
) -> bool:
    if not capture_tree(entry):
        return False
    entry.state, entry.interrupt_requested_at = "interrupt_requested", time.time()
    if interrupt_fn:
        # Adapter interrupts themselves must be bounded; callers may run them
        # asynchronously. Their response never substitutes for tree verification.
        threading.Thread(target=interrupt_fn, args=(entry,), daemon=True).start()
    else:
        for pid, created in {entry.pid: entry.create_time, **entry.descendants}.items():
            if process_state(pid, created) == "alive":
                with contextlib.suppress(psutil.Error):
                    psutil.Process(pid).terminate()
    if wait_descendants_stopped(entry, cooperative_seconds):
        entry.state, entry.finished_at = "finished", time.time()
        return True
    capture_tree(entry)
    if kill_fn:
        threading.Thread(target=kill_fn, args=(entry,), daemon=True).start()
    if entry.group:
        entry.group.close()
    for pid, created in entry.descendants.items() | {(entry.pid, entry.create_time)}:
        if process_state(pid, created) == "alive":
            with contextlib.suppress(psutil.Error):
                psutil.Process(pid).kill()
    entry.state, entry.killed_at = "killed", time.time()
    stopped = wait_descendants_stopped(entry, kill_seconds)
    if stopped:
        entry.state, entry.finished_at = "finished", time.time()
    return stopped


def discover_descendants(root_pid: int) -> list[psutil.Process]:
    try:
        root = psutil.Process(root_pid)
    except psutil.NoSuchProcess:
        return []
    try:
        return root.children(recursive=True)
    except psutil.Error:
        return []


def stop_owned(
    registry: ProcessRegistry,
    owner_generation: int,
    *,
    cooperative_seconds: float = 10.0,
    kill_seconds: float = 5.0,
) -> bool:
    """Interrupt then kill every registered descendant of this worker.

    Returns ``True`` only when every owned PID is confirmed stopped.
    Foreign PIDs and other generations are never touched.
    """

    entries = list(registry.active(owner_generation))
    stopped = True
    for entry in entries:
        if interrupt(entry, cooperative_seconds=cooperative_seconds, kill_seconds=kill_seconds):
            registry.mark_finished(entry.pid, owner_generation, create_time=entry.create_time)
        else:
            stopped = False
    return stopped


def health(registry: ProcessRegistry, owner_generation: int) -> dict[str, Any]:
    """Compact health view: active count, oldest started_at, stale entries.

    The view is read-only; no process is signalled.
    """

    entries = registry.active(owner_generation)
    alive = [
        entry
        for entry in entries
        if entry.process is not None and is_alive_pid(entry.pid, entry.create_time)
    ]
    finished = [entry for entry in entries if entry not in alive]
    return {
        "owner_generation": owner_generation,
        "active": len(alive),
        "registered": len(entries),
        "finished": len(finished),
        "oldest_started_at": min((entry.started_at for entry in alive), default=None),
    }


def stored_processes_stopped(session: Session, run: Run) -> bool:
    call_owner = json.loads(run.runtime_json).get("active_call_owner")
    if call_owner and process_state(call_owner["pid"], call_owner["create_time"]) != "dead":
        return False
    target = json.loads(run.resume_target_json or "{}")
    previous = target.get("previous_owner")
    if previous and (
        not previous.get("pid")
        or not previous.get("create_time")
        or process_state(previous["pid"], previous["create_time"]) != "dead"
    ):
        return False
    for row in session.scalars(
        select(ProcessSupervision).where(ProcessSupervision.run_id == run.id)
    ):
        tree = json.loads(row.tree_json or "{}")
        if process_state(row.pid, row.create_time) != "dead":
            return False
        if any(
            process_state(int(pid), created) != "dead"
            for pid, created in tree.get("pids", {}).items()
        ):
            return False
        # Legacy rows did not capture children or prove Job ownership.
        if not tree.get("job_owned") and not tree.get("tree_verified"):
            return False
    return True


def recovery_evidence(
    factory: sessionmaker[Session], run_id: str, target: dict[str, Any]
) -> dict[str, Any]:
    with factory() as session:
        run = session.get(Run, run_id)
        assert run is not None
        stopped = stored_processes_stopped(session, run)
        rows = list(
            session.scalars(select(ProcessSupervision).where(ProcessSupervision.run_id == run_id))
        )
        processes = [
            {"id": row.id, "pid": row.pid, "status": process_state(row.pid, row.create_time)}
            for row in rows
        ]
    return {
        "stopped": stopped,
        "processes": processes,
        "previous_owner": target.get("previous_owner"),
        "checked_at": utc_now(),
    }


class ProcessSupervisor:
    """One owned Job per launch; persist its intent before allowing target code to run."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        registry: ProcessRegistry,
        run_id: str,
        worker_id: str,
        generation: int,
    ) -> None:
        self.factory, self.registry = factory, registry
        self.run_id, self.worker_id, self.generation = run_id, worker_id, generation

    def _register_callback(
        self,
        group: ProcessGroup,
        holder: dict[str, ProcessRegistryEntry],
        *,
        role: str,
        kind: str,
        attempt_id: str | None,
        transport: str,
        port: int | None,
    ) -> Callable[[psutil.Process], None]:
        def register(process: psutil.Process) -> None:
            entry = self.registry.register(
                process=process,
                kind=kind,
                role=role,
                owner_generation=self.generation,
                run_id=self.run_id,
                step_attempt_id=attempt_id,
            )
            entry.group = group
            holder["entry"] = entry
            with self.factory() as session:
                begin_write(session)
                owned_job(session, self.run_id, self.worker_id, self.generation)
                run = session.get(Run, self.run_id)
                assert run is not None
                if kind in {"command", "git"}:
                    from agents_ide.security.filesystem import directory_identity

                    workspace = json.loads(run.snapshot_json)["workspace"]
                    root = Path(workspace["workspace_path"])
                    identity = directory_identity(root)
                    if identity != (workspace["identity_dev"], workspace["identity_ino"]):
                        raise AppError(
                            "workspace_conflict", "Workspace changed before dispatch", 409
                        )
                continuing_step = (
                    kind in {"command", "git"}
                    and attempt_id is not None
                    and run.current_attempt_id == attempt_id
                    and run.state == "pause_requested"
                )
                allowed_states = (
                    {"running", "queued", "recovering"} if kind == "git" else {"running", "queued"}
                )
                if run.state not in allowed_states and not continuing_step:
                    raise AppError("dispatch_blocked", "Управление запретило запуск процесса", 409)
                session.add(
                    ProcessSupervision(
                        id=new_id(),
                        run_id=self.run_id,
                        step_attempt_id=attempt_id,
                        role=role,
                        kind=kind,
                        owner_generation=self.generation,
                        pid=entry.pid,
                        create_time=entry.create_time,
                        parent_pid=entry.parent_pid,
                        executable=entry.executable,
                        started_at=entry.started_at,
                        state="started",
                        tree_json=to_json({"job_owned": sys.platform == "win32", "pids": {}}),
                        workspace_json=to_json(json.loads(run.snapshot_json)["workspace"]),
                        transport=transport,
                        port=port,
                    )
                )
                session.commit()

        return register

    def start(
        self,
        argv: list[str],
        cwd: Path,
        env: dict[str, str],
        *,
        role: str = "support",
        kind: str = "support",
        attempt_id: str | None = None,
        transport: str = "process",
        port: int | None = None,
    ) -> ProcessRegistryEntry:
        if port is not None:
            import socket

            try:
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", port))
            except (OSError, OverflowError):
                raise AppError(
                    "port_unavailable",
                    "Порт занят или недоступен; чужой процесс не используется",
                    409,
                ) from None
        group = ProcessGroup()
        holder: dict[str, ProcessRegistryEntry] = {}
        callback = self._register_callback(
            group,
            holder,
            role=role,
            kind=kind,
            attempt_id=attempt_id,
            transport=transport,
            port=port,
        )
        try:
            group.start(argv, cwd, env, before_resume=callback)
        except BaseException:
            group.close()
            entry = holder.get("entry")
            if entry:
                self.registry.mark_finished(
                    entry.pid, self.generation, create_time=entry.create_time
                )
            raise
        entry = holder.get("entry")
        assert entry is not None
        return entry

    def start_stdio(
        self,
        argv: list[str],
        cwd: Path,
        env: dict[str, str],
        *,
        role: str = "command",
        kind: str = "command",
        attempt_id: str | None = None,
        stdin: int = subprocess.DEVNULL,
    ) -> tuple[ProcessRegistryEntry, subprocess.Popen[str]]:
        """Start an owned process with captured stdio; used by Command nodes."""

        group = ProcessGroup()
        holder: dict[str, ProcessRegistryEntry] = {}
        callback = self._register_callback(
            group,
            holder,
            role=role,
            kind=kind,
            attempt_id=attempt_id,
            transport="process",
            port=None,
        )
        try:
            child = group.popen_stdio(argv, cwd, env, before_resume=callback, stdin=stdin)
        except BaseException:
            group.close()
            entry = holder.get("entry")
            if entry:
                self.registry.mark_finished(
                    entry.pid, self.generation, create_time=entry.create_time
                )
            raise
        entry = holder.get("entry")
        assert entry is not None
        return entry, child

    def refresh_health(self, *, external_event: bool = False) -> None:
        entries = self.registry.by_run(self.run_id)
        for entry in entries:
            if entry.owner_generation != self.generation:
                continue
            verified = capture_tree(entry)
            finished = False
            with self.factory() as session:
                begin_write(session)
                owned_job(session, self.run_id, self.worker_id, self.generation)
                row = session.scalar(
                    select(ProcessSupervision).where(
                        ProcessSupervision.run_id == self.run_id,
                        ProcessSupervision.pid == entry.pid,
                        ProcessSupervision.owner_generation == self.generation,
                        ProcessSupervision.create_time == entry.create_time,
                    )
                )
                if row:
                    row.tree_json = to_json(
                        {
                            "job_owned": entry.group is not None and sys.platform == "win32",
                            "tree_verified": verified,
                            "pids": entry.descendants,
                        }
                    )
                    if process_state(entry.pid, entry.create_time) == "alive":
                        row.last_health_ok = utc_now()
                    if external_event:
                        row.last_external_event_at = utc_now()
                    if wait_descendants_stopped(entry, 0):
                        row.state, row.finished_at = "finished", utc_now()
                        finished = True
                session.commit()
            if finished:
                if entry.group:
                    entry.group.close()
                self.registry.mark_finished(
                    entry.pid, self.generation, create_time=entry.create_time
                )

    def stop(self, *, cooperative_seconds: float = 10, kill_seconds: float = 5) -> bool:
        stopped = True
        cooperative_deadline = time.monotonic() + cooperative_seconds
        kill_deadline = cooperative_deadline + kill_seconds
        for entry in self.registry.by_run(self.run_id):
            if entry.owner_generation != self.generation:
                continue
            result = interrupt(
                entry,
                interrupt_fn=(lambda _: True)
                if entry.interrupt_requested_at
                else entry.interrupt_callback,
                cooperative_seconds=max(0, cooperative_deadline - time.monotonic()),
                kill_seconds=max(0, kill_deadline - max(time.monotonic(), cooperative_deadline)),
            )
            stopped = stopped and result
            try:
                with self.factory() as session:
                    begin_write(session)
                    owned_job(session, self.run_id, self.worker_id, self.generation)
                    row = session.scalar(
                        select(ProcessSupervision).where(
                            ProcessSupervision.run_id == self.run_id,
                            ProcessSupervision.pid == entry.pid,
                            ProcessSupervision.owner_generation == self.generation,
                            ProcessSupervision.create_time == entry.create_time,
                        )
                    )
                    if row:
                        row.state = "finished" if result else "unknown"
                        row.finished_at = utc_now() if result else None
                        row.interrupt_requested_at, row.killed_at = (
                            entry.interrupt_requested_at,
                            entry.killed_at,
                        )
                        row.tree_json = to_json(
                            {
                                "job_owned": sys.platform == "win32",
                                "tree_verified": True,
                                "pids": entry.descendants,
                            }
                        )
                    session.commit()
            except Exception:
                # Losing the DB/lease cannot prevent local shutdown. Recovery will
                # inspect persisted identities; an old owner cannot commit evidence.
                pass
            if result:
                if entry.group:
                    entry.group.close()
                self.registry.mark_finished(
                    entry.pid, self.generation, create_time=entry.create_time
                )
        return stopped

    def request_interrupt(self, callback: Callable[[ProcessRegistryEntry], bool]) -> None:
        """Request a native interrupt; stop still verifies the whole tree."""
        for entry in self.registry.by_run(self.run_id):
            if entry.owner_generation == self.generation:
                entry.interrupt_callback = callback
                entry.state, entry.interrupt_requested_at = "interrupt_requested", time.time()
                threading.Thread(target=callback, args=(entry,), daemon=True).start()

    def is_running(self) -> bool:
        return any(
            not wait_descendants_stopped(entry, 0)
            for entry in self.registry.by_run(self.run_id)
            if entry.owner_generation == self.generation
        )

    def cleanup_stale(self) -> dict[str, Any]:
        with self.factory() as session:
            owned_job(session, self.run_id, self.worker_id, self.generation)
            run = session.get(Run, self.run_id)
            assert run is not None
            target = json.loads(run.resume_target_json or "{}")
        evidence = recovery_evidence(self.factory, self.run_id, target)
        if not evidence["stopped"]:
            raise AppError("process_not_responding", "Нельзя очистить неподтверждённое дерево", 409)
        with self.factory() as session:
            begin_write(session)
            owned_job(session, self.run_id, self.worker_id, self.generation)
            for row in session.scalars(
                select(ProcessSupervision).where(
                    ProcessSupervision.run_id == self.run_id,
                    ProcessSupervision.owner_generation < self.generation,
                    ProcessSupervision.finished_at.is_(None),
                )
            ):
                row.state, row.finished_at = "finished", utc_now()
            session.commit()
        return evidence
