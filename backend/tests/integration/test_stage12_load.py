"""Reproducible 100k-event fixture; writes actual p95 metrics for release acceptance."""

import asyncio
import json
import math
import os
import platform
import time
from pathlib import Path

from sqlalchemy import insert
from test_stage4_review import make_run

from agents_ide.engine.artifacts import ArtifactPayload, record_artifact
from agents_ide.engine.events import append_event
from agents_ide.engine.events_stream import MAX_BUFFER_BYTES, StreamHub
from agents_ide.persistence.models import Run, RunEvent


def p95(values):
    return sorted(values)[math.ceil(len(values) * 0.95) - 1]


def test_100k_events_large_artifacts_pagination_and_slow_observer(
    authenticated, tmp_path, settings
):
    client, _ = authenticated
    run, factory = make_run(authenticated, tmp_path)
    now = time.time()
    # Bulk fixture loading is deliberately separate from timed requests. IDs and
    # sequences have the same uniqueness constraints as normal persisted events.
    for start in range(2, 100002, 2000):
        with factory() as session:
            session.execute(
                insert(RunEvent),
                [
                    {
                        "id": f"load-{n}",
                        "run_id": run["id"],
                        "sequence": n,
                        "event_version": 1,
                        "type": "agent.message_delta",
                        "occurred_at": now,
                        "persisted_at": now,
                        "worker_generation": 1,
                        "payload_json": json.dumps({"text": "streaming output " + "x" * 180}),
                    }
                    for n in range(start, start + 2000)
                ],
            )
            session.commit()
    with factory() as session:
        # More than the payload cap exercises truncation and fragmented reads.
        artifact = record_artifact(
            session, run["id"], ArtifactPayload("large_output", body="x" * 12000000)
        )
        session.get(Run, run["id"]).detailed_event_count = 100000
        session.commit()
    url = f"/api/runs/{run['id']}"
    snapshots, history, artifact_pages = [], [], []
    for _ in range(25):
        started = time.perf_counter()
        response = client.get(url + "/snapshot")
        snapshots.append((time.perf_counter() - started) * 1000)
        assert response.status_code == 200
        started = time.perf_counter()
        response = client.get(url + "/history?limit=200")
        history.append((time.perf_counter() - started) * 1000)
        assert len(response.json()["events"]) == 200
        started = time.perf_counter()
        response = client.get(url + f"/artifacts/{artifact.id}/content?limit=16000")
        artifact_pages.append((time.perf_counter() - started) * 1000)
        assert response.status_code == 200 and len(response.json()["text"]) <= 16000
    # Read the entire retained history using keyset pagination, no gaps/duplicates.
    before, total = None, 0
    while True:
        response = client.get(
            url + "/history?limit=200" + (f"&before={before}" if before else "")
        ).json()
        seq = [event["sequence"] for event in response["events"]]
        assert all(b == a - 1 for a, b in zip(seq, seq[1:], strict=False))
        if before and seq:
            assert seq[0] == before - 1
        total += len(seq)
        before = response["next_before"]
        if not response["has_more"]:
            break
    assert total == 100001

    async def delivery():
        hub = StreamHub(factory)
        fast = await hub.subscribe(run["id"])
        slow = await hub.subscribe(run["id"])
        latencies = []
        try:
            for index in range(25):
                # Idle backoff must still meet the active subscriber delivery SLO.
                await asyncio.sleep(0.55)
                with factory() as session:
                    append_event(session, run["id"], "command.finished", {"index": index})
                    session.commit()
                started = time.perf_counter()
                batch = await asyncio.wait_for(fast.get(), 2)
                latencies.append((time.perf_counter() - started) * 1000)
                assert batch.events[-1]["payload"]["index"] == index
            # Stop reading one subscriber, while the fast one consumes every
            # persisted batch. Large critical events bypass only the detail quota.
            for _ in range(200):
                with factory() as session:
                    append_event(session, run["id"], "command.finished", {"text": "x" * 15000})
                    session.commit()
                await asyncio.wait_for(fast.get(), 2)
                assert fast.buffered_bytes <= MAX_BUFFER_BYTES
            assert slow.overflow and slow.queue.qsize() == 1
            assert (await slow.get()).reset_required
            return latencies
        finally:
            await hub.close()
            assert not hub.tasks and not hub.subscribers

    latency = asyncio.run(delivery())
    report = {
        "profile": {
            "os": platform.platform(),
            "python": platform.python_version(),
            "processor": platform.processor(),
            "storage": "local filesystem; SSD requires host confirmation",
        },
        "event_fixture": 100000,
        "large_output_input_bytes": 12000000,
        "samples": 25,
        "p95_ms": {
            "snapshot": p95(snapshots),
            "history_first_page": p95(history),
            "artifact_chunk": p95(artifact_pages),
            "persisted_to_subscription": p95(latency),
        },
        "buffers": {"limit_bytes": MAX_BUFFER_BYTES, "slow_reset": True},
        "delivery_scope": "API StreamHub subscription; browser render timing is separate",
    }
    destination = Path(os.environ.get("AGENTS_IDE_LOAD_REPORT", str(tmp_path / "load-report.json")))
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    assert report["p95_ms"]["snapshot"] < 500
    assert report["p95_ms"]["history_first_page"] < 1000
    assert report["p95_ms"]["persisted_to_subscription"] < 1000
