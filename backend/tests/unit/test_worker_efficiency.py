"""Regression coverage for measured Windows worker hot paths."""

import os
import tracemalloc
from types import SimpleNamespace

import psutil
import pytest

from agents_ide.engine import ownership
from agents_ide.security.workspace_read import read_workspace_file
from agents_ide.worker import processes, windows_jobs


def test_small_workspace_read_does_not_allocate_the_manifest_budget(tmp_path):
    body = b"small source file" * 64
    (tmp_path / "source.txt").write_bytes(body)
    # Warm platform imports before measuring the read itself.
    assert read_workspace_file(tmp_path, "source.txt", 64 * 1024**2) == body
    tracemalloc.start()
    try:
        assert read_workspace_file(tmp_path, "source.txt", 64 * 1024**2) == body
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1024**2


@pytest.mark.parametrize("body", [b"", b"content"])
def test_workspace_read_accepts_exact_cap(tmp_path, body):
    (tmp_path / "source.txt").write_bytes(body)
    assert read_workspace_file(tmp_path, "source.txt", len(body)) == body


def test_workspace_read_still_rejects_oversize_file(tmp_path):
    (tmp_path / "source.txt").write_bytes(b"content")
    with pytest.raises(ValueError, match="too_large"):
        read_workspace_file(tmp_path, "source.txt", 3)


@pytest.mark.parametrize("replacement", [b"", b"content grew"])
def test_workspace_read_detects_size_change_after_initial_stat(tmp_path, monkeypatch, replacement):
    path = tmp_path / "source.txt"
    path.write_bytes(b"content")
    original = os.fstat
    changed = False

    def fstat(fd):
        nonlocal changed
        result = original(fd)
        if not changed:
            changed = True
            path.write_bytes(replacement)
        return result

    monkeypatch.setattr(os, "fstat", fstat)
    with pytest.raises(ValueError, match="unstable_file"):
        read_workspace_file(tmp_path, "source.txt", 64 * 1024**2)


def test_windows_liveness_does_not_inspect_thread_suspension(monkeypatch):
    monkeypatch.setattr(windows_jobs, "process_exited", lambda _: False)
    def status():
        pytest.fail("Windows liveness must not enumerate process threads")

    process = SimpleNamespace(create_time=lambda: 1.0, is_running=lambda: True, status=status)
    for module in (ownership, processes):
        monkeypatch.setattr(module, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(psutil, "Process", lambda *_: process)
    assert processes.is_running(process)
    assert processes.process_state(123, 1.0) == "alive"
    assert ownership.owner_may_be_alive(123, 1.0)
    assert processes.process_state(123, 2.0) == "dead"
    assert not ownership.owner_may_be_alive(123, 2.0)


def test_unix_zombie_is_still_treated_as_dead(monkeypatch):
    process = SimpleNamespace(
        create_time=lambda: 1.0, is_running=lambda: True, status=lambda: psutil.STATUS_ZOMBIE
    )
    for module in (ownership, processes):
        monkeypatch.setattr(module, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(psutil, "Process", lambda *_: process)
    assert not processes.is_running(process)
    assert processes.process_state(123, 1.0) == "dead"
    assert not ownership.owner_may_be_alive(123, 1.0)


@pytest.mark.parametrize(
    "error,expected", [(psutil.NoSuchProcess, "dead"), (psutil.AccessDenied, "unknown")]
)
def test_process_inspection_failure_remains_conservative(monkeypatch, error, expected):
    monkeypatch.setattr(windows_jobs, "process_exited", lambda _: False)
    def process(_):
        raise error(123)

    monkeypatch.setattr(psutil, "Process", process)
    assert processes.process_state(123, 1.0) == expected
    assert ownership.owner_may_be_alive(123, 1.0) == (expected == "unknown")


def test_windows_confirmed_exit_does_not_scan_system_processes(monkeypatch):
    monkeypatch.setattr(processes, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(windows_jobs, "process_exited", lambda _: True)
    monkeypatch.setattr(psutil, "Process", lambda *_: pytest.fail("Unexpected system process scan"))
    assert processes.process_state(123, 1.0) == "dead"


@pytest.mark.parametrize("created,expected", [(1.0, "alive"), (2.0, "dead"), (None, "unknown")])
def test_windows_sync_denial_still_checks_for_recycled_pid(monkeypatch, created, expected):
    monkeypatch.setattr(processes, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(windows_jobs, "process_exited", lambda _: None)

    def inspect(_):
        if created is None:
            raise psutil.AccessDenied(123)
        return SimpleNamespace(create_time=lambda: created, is_running=lambda: True)

    monkeypatch.setattr(psutil, "Process", inspect)
    assert processes.process_state(123, 1.0) == expected


def test_new_child_registration_uses_known_parent_without_system_process_scan():
    process = SimpleNamespace(
        pid=123,
        create_time=lambda: 1.0,
        exe=lambda: "git.exe",
        ppid=lambda: pytest.fail("The launcher already knows the parent PID"),
    )
    entry = processes.ProcessRegistry().register(
        process=process,
        kind="git",
        role="git",
        owner_generation=1,
        run_id="run",
        step_attempt_id="attempt",
        parent_pid=os.getpid(),
    )
    assert entry.parent_pid == os.getpid()
