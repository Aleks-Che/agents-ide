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
