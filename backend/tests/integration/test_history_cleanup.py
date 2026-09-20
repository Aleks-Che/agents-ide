import json
import sqlite3

import pytest
from sqlalchemy import select
from test_stage4_review import make_run

from agents_ide.engine.artifacts import ArtifactPayload, record_artifact
from agents_ide.engine.events import append_event
from agents_ide.operations.history import compact_history
from agents_ide.persistence.models import ArtifactManifest, Run, RunEvent
from agents_ide.services.transactions import begin_write


def populate(authenticated, tmp_path):
    run, factory = make_run(authenticated, tmp_path)
    pinned, _ = make_run(authenticated, tmp_path, suffix="pinned")
    with factory() as session:
        begin_write(session)
        session.get(Run, pinned["id"]).pinned = True
        removed = record_artifact(
            session,
            run["id"],
            ArtifactPayload(
                "event_payload",
                body={
                    "native_type": "message.part.updated",
                    "output": "tool output" * 10_000,
                },
            ),
        )
        child = record_artifact(
            session,
            run["id"],
            ArtifactPayload(
                "event_payload",
                body={
                    "question": "referenced indirectly",
                },
            ),
        )
        protected = record_artifact(
            session,
            run["id"],
            ArtifactPayload(
                "event_payload",
                body={
                    "question": "keep",
                    "artifact_id": child.id,
                },
            ),
        )
        final = record_artifact(
            session,
            run["id"],
            ArtifactPayload(
                "agent_response",
                body={
                    "raw_text": "Final answer",
                    "validated": {"report": "Final answer"},
                },
            ),
        )
        for _ in range(1000):
            append_event(session, run["id"], "agent.native_event", {"artifact_id": removed.id})
            append_event(session, run["id"], "attempt.text_delta", {"text": "Привет 😀"})
        append_event(session, run["id"], "agent.input_requested", {"artifact_id": protected.id})
        append_event(session, run["id"], "attempt.text_delta", {"text": " tail"})
        append_event(session, run["id"], "attempt.finished", {"result_ref": final.id})
        append_event(session, pinned["id"], "agent.native_event", {"text": "pinned diagnostic"})
        session.commit()
        ids = {
            "removed": removed.id,
            "protected": protected.id,
            "child": child.id,
            "final": final.id,
        }
    return run, pinned, factory, ids


def test_cleanup_preserves_answers_controls_results_references_and_pinned_history(
    authenticated, tmp_path, settings
):
    run, pinned, factory, ids = populate(authenticated, tmp_path)
    with factory() as session:
        high = max(session.scalars(select(RunEvent.sequence).where(RunEvent.run_id == run["id"])))
        critical = {
            row.id: row.payload_json
            for row in session.scalars(
                select(RunEvent).where(
                    RunEvent.run_id == run["id"],
                    RunEvent.type.in_(["agent.input_requested", "attempt.finished"]),
                )
            )
        }
    with sqlite3.connect(settings.database_path) as db:
        stats = compact_history(db)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert stats["events_removed"] == 1000 and stats["text_events_merged"] == 999
    assert stats["artifact_bytes_purged"] > 100_000
    with factory() as session:
        rows = list(
            session.scalars(
                select(RunEvent).where(RunEvent.run_id == run["id"]).order_by(RunEvent.sequence)
            )
        )
        assert (
            "".join(
                json.loads(row.payload_json)["text"]
                for row in rows
                if row.type == "attempt.text_delta"
            )
            == "Привет 😀" * 1000 + " tail"
        )
        assert all(row.payload_json == critical[row.id] for row in rows if row.id in critical)
        assert rows[-1].type == "run.history_compacted" and rows[-1].sequence == high + 1
        assert session.get(ArtifactManifest, ids["removed"]).body_json is None
        for name in ["protected", "child", "final"]:
            assert session.get(ArtifactManifest, ids[name]).body_json is not None
        assert session.scalar(
            select(RunEvent).where(
                RunEvent.run_id == pinned["id"], RunEvent.type == "agent.native_event"
            )
        )
        assert session.get(Run, run["id"]).detailed_event_count == 2
    with sqlite3.connect(settings.database_path) as db:
        assert compact_history(db) == {
            "runs": 0,
            "events_removed": 0,
            "text_events_merged": 0,
            "artifact_bytes_purged": 0,
        }


def test_interrupted_cleanup_rolls_back_all_history_changes(authenticated, tmp_path, settings):
    run, _, factory, ids = populate(authenticated, tmp_path)

    def fail(_):
        raise OSError("abort maintenance")

    with sqlite3.connect(settings.database_path) as db:
        before = db.execute("SELECT count(*) FROM run_events").fetchone()[0]
        with pytest.raises(OSError, match="abort maintenance"):
            compact_history(db, progress=fail)
        assert db.execute("SELECT count(*) FROM run_events").fetchone()[0] == before
    with factory() as session:
        assert session.get(ArtifactManifest, ids["removed"]).body_json is not None
        session.get(Run, run["id"]).state = "running"
        session.commit()
    with (
        sqlite3.connect(settings.database_path) as db,
        pytest.raises(ValueError, match="Active runs"),
    ):
        compact_history(db)
