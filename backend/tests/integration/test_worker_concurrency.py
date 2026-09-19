"""The actual worker admits every independent queued run before any finishes."""

import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select
from test_stage4_review import make_run
from test_stage5_review import wait_for

from agents_ide import launcher
from agents_ide.adapters.base import ExternalOutcome, LLMResult
from agents_ide.engine.runner import Runner
from agents_ide.persistence.models import QueueJob, Run, StepAttempt
from agents_ide.worker import main


def test_worker_starts_six_dialogs_without_waiting_for_other_runs(
    authenticated, tmp_path, settings, monkeypatch
):
    runs = []
    for index in range(6):
        workspace = tmp_path / f"workspace{index}"
        workspace.mkdir()
        subprocess.run(["git", "init", str(workspace)], check=True, capture_output=True)
        run, factory = make_run(authenticated, tmp_path, suffix=str(index))
        runs.append(run)
    release, stop = threading.Event(), threading.Event()
    active = set()
    guard = threading.Lock()

    class Adapter:
        def run(self, request):
            with guard:
                active.add(threading.get_ident())
            assert release.wait(20), "Worker serialized independent runs"
            return LLMResult(ExternalOutcome.SUCCEEDED, "ok", {"text": "ok"}, None)

    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (None, Adapter()))
    monkeypatch.setattr(main.signal, "signal", lambda *_: None)
    monkeypatch.setattr(launcher, "stop_requested", lambda *_: stop.is_set())
    settings.heartbeat_seconds = 0.1
    with ThreadPoolExecutor(1) as executor:
        worker = executor.submit(main.run_worker, settings)
        try:
            wait_for(factory, lambda _: len(active) == len(runs), timeout=15)
            with factory() as session:
                assert (
                    len(
                        list(
                            session.scalars(select(QueueJob).where(QueueJob.claimed_by.isnot(None)))
                        )
                    )
                    == 6
                )
                assert (
                    len(
                        list(
                            session.scalars(
                                select(StepAttempt).where(StepAttempt.status == "running")
                            )
                        )
                    )
                    == 6
                )
            release.set()
            wait_for(
                factory,
                lambda s: all(s.get(Run, r["id"]).state == "completed" for r in runs),
                timeout=15,
            )
        finally:
            release.set()
            stop.set()
            worker.result(timeout=15)
