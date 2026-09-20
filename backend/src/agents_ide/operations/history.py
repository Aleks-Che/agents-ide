"""Explicit offline cleanup of disposable telemetry; keep answers and control history.

The caller must hold maintenance.offline and create a verified backup first.
This operation uses one transaction and never rewrites final result artifacts.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from collections.abc import Callable
from typing import Any

from agents_ide.adapters.history import OMITTED_HISTORY_EVENTS, tool_summaries
from agents_ide.domain.common import to_json
from agents_ide.engine.stream_buffer import BLOCK_BYTES

_IDS = re.compile(r"(?<![a-fA-F0-9])[a-fA-F0-9]{32}(?![a-fA-F0-9])")
_TEXT = {"attempt.text_delta", "agent.message_delta"}
_SCOPE = (
    "type",
    "node_id",
    "step_execution_id",
    "step_attempt_id",
    "agent_session_id",
    "command_id",
    "worker_generation",
    "event_version",
)


def _payload(db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(row["payload_json"])
    if body.get("artifact_id"):
        artifact = db.execute(
            "SELECT body_json FROM artifact_manifests WHERE id=? AND run_id=?",
            (body["artifact_id"], row["run_id"]),
        ).fetchone()
        if artifact and artifact[0]:
            value = json.loads(artifact[0])
            if isinstance(value, dict):
                return value
    return body


def _protect_recovery(db: sqlite3.Connection, run: sqlite3.Row) -> None:
    if run["state"] not in {"paused", "stopped", "waiting_input"} or not run["current_attempt_id"]:
        return
    attempt = db.execute(
        "SELECT error_details_json FROM step_attempts WHERE id=?", (run["current_attempt_id"],)
    ).fetchone()
    if attempt is None:
        return
    rows = db.execute(
        "SELECT * FROM run_events WHERE run_id=? AND step_attempt_id=? "
        "AND type='agent.tool_call' ORDER BY sequence DESC LIMIT 20",
        (run["id"], run["current_attempt_id"]),
    ).fetchall()
    if rows:
        details = json.loads(attempt[0] or "{}")
        details["tool_summary"] = tool_summaries(_payload(db, row) for row in reversed(rows))
        db.execute(
            "UPDATE step_attempts SET error_details_json=? WHERE id=?",
            (to_json(details), run["current_attempt_id"]),
        )


def _referenced_payloads(db: sqlite3.Connection, candidates: set[str]) -> set[str]:
    """Scan surviving references once, rather than scanning the DB per artifact."""
    protected: set[str] = set()

    def scan(rows: Any) -> None:
        for row in rows:
            for value in row:
                if isinstance(value, str):
                    protected.update(candidates.intersection(_IDS.findall(value)))

    for table, columns in (
        ("step_executions", "raw_result_ref,evidence_manifest_id,validated_result_json"),
        ("step_attempts", "request_artifact_id,result_artifact_id,error_details_json"),
        ("plan_items", "evidence_ids_json"),
        ("runs", "runtime_json,snapshot_json,resume_target_json"),
        ("run_events", "payload_json"),
        ("command_journal", "payload_json,response_json"),
    ):
        scan(db.execute(f"SELECT {columns} FROM {table}"))
    scan(
        db.execute(
            "SELECT body_json,files_json FROM artifact_manifests "
            "WHERE id NOT IN (SELECT id FROM history_payload_candidates)"
        )
    )
    # A retained payload can itself reference another candidate.
    visited: set[str] = set()
    while pending := protected - visited:
        for artifact_id in pending:
            scan(
                db.execute(
                    "SELECT body_json,files_json FROM artifact_manifests WHERE id=?", (artifact_id,)
                )
            )
        visited.update(pending)
    return protected


def compact_history(
    db: sqlite3.Connection, *, progress: Callable[[dict[str, Any]], None] | None = None
) -> dict[str, int]:
    if db.in_transaction:
        raise ValueError("History cleanup requires its own transaction")
    db.row_factory = sqlite3.Row
    counts = {"runs": 0, "events_removed": 0, "text_events_merged": 0, "artifact_bytes_purged": 0}
    db.execute("BEGIN IMMEDIATE")
    try:
        if db.execute(
            "SELECT 1 FROM runs WHERE state IN "
            "('running','pause_requested','stop_requested','recovering','retry_wait') LIMIT 1"
        ).fetchone():
            raise ValueError("Active runs must stop before history cleanup")
        runs = db.execute("SELECT id,state,current_attempt_id FROM runs WHERE pinned=0").fetchall()
        for run in runs:
            _protect_recovery(db, run)
            original_digest = hashlib.sha256()
            pending: dict[str, Any] | None = None
            removed: list[tuple[str]] = []
            discarded = merged = after = 0
            high = (
                db.execute(
                    "SELECT max(sequence) FROM run_events WHERE run_id=?", (run["id"],)
                ).fetchone()[0]
                or 0
            )

            def flush() -> None:
                nonlocal pending
                if pending and pending["changed"]:
                    body = {
                        **pending["metadata"],
                        pending["field"]: "".join(pending["parts"]),
                        "coalesced_chunks": pending["count"],
                    }
                    db.execute(
                        "UPDATE run_events SET payload_json=? WHERE id=?",
                        (to_json(body), pending["id"]),
                    )
                pending = None

            while True:
                rows = db.execute(
                    "SELECT * FROM run_events WHERE run_id=? AND sequence>? "
                    "ORDER BY sequence LIMIT 1000",
                    (run["id"], after),
                ).fetchall()
                if not rows:
                    break
                for row in rows:
                    after = row["sequence"]
                    if row["type"] in OMITTED_HISTORY_EVENTS:
                        removed.append((row["id"],))
                        discarded += 1
                        continue
                    if row["type"] not in _TEXT:
                        flush()
                        continue
                    body = _payload(db, row)
                    field = "text" if isinstance(body.get("text"), str) else "delta"
                    if not isinstance(body.get(field), str):
                        flush()
                        continue
                    text = body[field]
                    original_digest.update(text.encode("utf-8"))
                    metadata = {
                        k: v for k, v in body.items() if k not in {field, "coalesced_chunks"}
                    }
                    scope = tuple(row[key] for key in _SCOPE)
                    count = body.get("coalesced_chunks", 1)
                    text_bytes = len(to_json(text).encode("utf-8")) - 2
                    if (
                        pending
                        and pending["scope"] == scope
                        and pending["metadata"] == metadata
                        and pending["field"] == field
                    ):
                        size = len(
                            to_json(
                                {
                                    **metadata,
                                    field: "",
                                    "coalesced_chunks": pending["count"] + count,
                                }
                            ).encode("utf-8")
                        )
                        if size + pending["text_bytes"] + text_bytes <= BLOCK_BYTES:
                            pending["parts"].append(text)
                            pending["text_bytes"] += text_bytes
                            pending["count"] += count
                            pending["changed"] = True
                            removed.append((row["id"],))
                            merged += 1
                            continue
                    flush()
                    pending = {
                        "id": row["id"],
                        "scope": scope,
                        "metadata": metadata,
                        "field": field,
                        "parts": [text],
                        "text_bytes": text_bytes,
                        "count": count,
                        "changed": False,
                    }
                db.executemany("DELETE FROM run_events WHERE id=?", removed)
                removed.clear()
            flush()
            retained_digest = hashlib.sha256()
            for row in db.execute(
                "SELECT * FROM run_events WHERE run_id=? AND type IN "
                "('attempt.text_delta','agent.message_delta') ORDER BY sequence",
                (run["id"],),
            ):
                body = _payload(db, row)
                text = body.get("text", body.get("delta"))
                if isinstance(text, str):
                    retained_digest.update(text.encode("utf-8"))
            if retained_digest.digest() != original_digest.digest():
                raise ValueError("History cleanup changed answer text")
            if discarded or merged:
                now = time.time()
                db.execute(
                    "INSERT INTO run_events (id,run_id,sequence,event_version,type,occurred_at,"
                    "persisted_at,worker_generation,payload_json) VALUES (?,?,?,1,?,?,?,?,?)",
                    (
                        uuid.uuid4().hex,
                        run["id"],
                        high + 1,
                        "run.history_compacted",
                        now,
                        now,
                        0,
                        to_json({"events_removed": discarded, "text_events_merged": merged}),
                    ),
                )
                db.execute(
                    "UPDATE runs SET detailed_event_count=(SELECT count(*) FROM run_events "
                    "WHERE run_id=? AND type IN ('agent.message_delta','attempt.text_delta',"
                    "'attempt.progress','agent.native_event','agent.output_delta')) WHERE id=?",
                    (run["id"], run["id"]),
                )
                counts["runs"] += 1
                counts["events_removed"] += discarded
                counts["text_events_merged"] += merged
            if progress:
                progress(dict(counts))
        db.execute("CREATE TEMP TABLE history_payload_candidates (id TEXT PRIMARY KEY)")
        db.execute(
            "INSERT INTO history_payload_candidates SELECT a.id FROM artifact_manifests a "
            "JOIN runs r ON r.id=a.run_id WHERE r.pinned=0 AND a.schema_type='event_payload' "
            "AND a.source_ref IS NULL AND a.plan_item_ids_json='[]' AND a.body_json IS NOT NULL"
        )
        candidates = {row[0] for row in db.execute("SELECT id FROM history_payload_candidates")}
        protected = _referenced_payloads(db, candidates)
        for artifact_id in candidates - protected:
            artifact = db.execute(
                "SELECT run_id,byte_length FROM artifact_manifests WHERE id=?", (artifact_id,)
            ).fetchone()
            db.execute(
                "UPDATE artifact_manifests SET body_json=NULL,purged_at=? WHERE id=?",
                (time.time(), artifact_id),
            )
            db.execute(
                "UPDATE runs SET artifact_bytes=max(0,artifact_bytes-?) WHERE id=?",
                (artifact[1], artifact[0]),
            )
            counts["artifact_bytes_purged"] += artifact[1]
        db.execute("DROP TABLE history_payload_candidates")
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return counts
