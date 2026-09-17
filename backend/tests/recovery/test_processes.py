import json
import os
import subprocess
import sys
import time

import psutil
import pytest

from agents_ide.worker.processes import ProcessGroup, wait_stopped


@pytest.mark.windows
@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object contract")
def test_job_kills_descendant_that_outlives_parent(tmp_path):
    output = tmp_path / "child.json"
    script = tmp_path / "parent.py"
    script.write_text(
        "import subprocess,sys,json,os,psutil\n"
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'])\n"
        f"open({str(output)!r},'w').write(json.dumps({{'pid':p.pid,'created':psutil.Process(p.pid).create_time()}}))\n"
        "os._exit(0)\n",
        encoding="utf-8",
    )
    group = ProcessGroup()
    descendant = None
    try:
        parent = group.start([sys.executable, str(script)], tmp_path, dict(os.environ))
        deadline = time.monotonic() + 10
        while not output.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert output.exists()
        info = json.loads(output.read_text())
        descendant = psutil.Process(info["pid"])
        assert descendant.create_time() == info["created"]
        assert wait_stopped([parent], 5)
        assert descendant.is_running()
    finally:
        group.close()
    assert descendant is not None and wait_stopped([descendant], 5)


@pytest.mark.windows
@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object owner crash")
def test_owner_crash_closes_job_and_kills_children(tmp_path):
    output = tmp_path / "child.json"
    script = tmp_path / "owner.py"
    script.write_text(
        "from agents_ide.worker.processes import ProcessGroup\n"
        "from pathlib import Path\nimport os,sys,json\n"
        "g=ProcessGroup()\n"
        "args=[sys.executable,'-c','import time; time.sleep(120)']\n"
        "p=g.start(args,Path.cwd(),dict(os.environ))\n"
        f"Path({str(output)!r}).write_text(json.dumps({{'pid':p.pid,'created':p.create_time()}}))\n"
        "os._exit(0)\n",
        encoding="utf-8",
    )
    result = subprocess.run([sys.executable, str(script)], capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr.decode()
    info = json.loads(output.read_text())
    try:
        child = psutil.Process(info["pid"])
        assert child.create_time() != info["created"] or wait_stopped([child], 5)
    except psutil.NoSuchProcess:
        pass


def test_owned_stdio_transport(tmp_path):
    group = ProcessGroup()
    child = group.popen_stdio(
        [sys.executable, "-c", "import sys; print(sys.stdin.readline().strip(), flush=True)"],
        tmp_path,
    )
    try:
        stdout, _ = child.communicate("protocol-ping\n", timeout=5)
        assert stdout.strip() == "protocol-ping"
        assert child.returncode == 0
    finally:
        group.close()


def test_job_retains_child_exit_code(tmp_path):
    group = ProcessGroup()
    try:
        child = group.start(
            [sys.executable, "-c", "raise SystemExit(7)"], tmp_path, dict(os.environ)
        )
        assert wait_stopped([child], 5)
        assert group.exit_code() == 7
    finally:
        group.close()


def test_capture_tree_tolerates_root_exit_during_inspection(monkeypatch):
    from types import SimpleNamespace

    from agents_ide.worker import processes

    entry = processes.ProcessRegistryEntry(
        pid=123,
        started_at=1,
        create_time=1,
        parent_pid=None,
        executable=None,
        kind="harness",
        role="agent",
        owner_generation=1,
        run_id="test",
        step_attempt_id=None,
    )
    monkeypatch.setattr(processes, "process_state", lambda *_: "alive")

    def children(**kwargs):
        raise psutil.NoSuchProcess(123)

    monkeypatch.setattr(psutil, "Process", lambda *_: SimpleNamespace(children=children))
    assert processes.capture_tree(entry)


def test_tree_stop_retries_transient_inspection_failure(monkeypatch):
    from agents_ide.worker import processes

    entry = processes.ProcessRegistryEntry(
        pid=123,
        started_at=1,
        create_time=1,
        parent_pid=None,
        executable=None,
        kind="harness",
        role="agent",
        owner_generation=1,
        run_id="test",
        step_attempt_id=None,
    )
    captures = iter([False, True])
    monkeypatch.setattr(processes, "capture_tree", lambda *_: next(captures))
    monkeypatch.setattr(processes, "process_state", lambda *_: "dead")
    assert processes.wait_descendants_stopped(entry, 0.5)
