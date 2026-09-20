import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from agents_ide.engine.stream_buffer import BLOCK_BYTES, StreamEventBuffer


def native(delta, harness="codex", item="item"):
    if harness == "codex":
        kind = "item/agentMessage/delta"
        payload = {
            "method": kind,
            "params": {"threadId": "s", "turnId": "t", "itemId": item, "delta": delta},
        }
    else:
        kind = "message.part.delta"
        payload = {
            "type": kind,
            "properties": {"sessionID": "s", "partID": item, "field": "text", "delta": delta},
        }
    return {"harness": harness, "native_type": kind, "session_id": "s", "payload": payload}


@pytest.mark.parametrize("harness", ["codex", "opencode"])
def test_interleaved_native_and_text_chunks_become_two_records_in_one_write(harness):
    batches = []
    buffer = StreamEventBuffer(batches.append, clock=lambda: 0)
    for _ in range(1000):
        buffer.emit("agent.native_event", native("a" * 60, harness))
        buffer.emit("attempt.text_delta", {"text": "a" * 60, "session_id": "s"})
    assert not batches
    buffer.close()
    assert len(batches) == 1 and len(batches[0]) == 2
    raw, text = [body for _, body in batches[0]]
    assert raw["coalesced_chunks"] == text["coalesced_chunks"] == 1000
    details = raw["payload"]["params" if harness == "codex" else "properties"]
    assert details["delta"] == text["text"] == "a" * 60_000


def test_boundaries_and_deadline_preserve_text_order_and_copy_payloads():
    batches, now = [], [0.0]
    buffer = StreamEventBuffer(batches.append, clock=lambda: now[0])
    payload = {"text": "before", "message_id": "m1"}
    buffer.emit("attempt.text_delta", payload)
    payload["text"] = "mutated"
    buffer.emit("agent.tool_call", {"call_id": "tool"})
    assert [kind for kind, _ in batches[0]] == ["attempt.text_delta", "agent.tool_call"]
    assert batches[0][0][1]["text"] == "before"
    buffer.emit("attempt.text_delta", {"text": "after", "message_id": "m1"})
    buffer.emit("attempt.text_delta", {"text": "second", "message_id": "m2"})
    now[0] = 1.9
    buffer.flush_due()
    assert len(batches) == 1
    now[0] = 2.0
    buffer.flush_due()  # A stalled provider still exposes the tail without another chunk.
    assert [body["text"] for _, body in batches[1]] == ["after", "second"]
    buffer.emit("attempt.text_delta", {"text": "tail"})
    buffer.emit("agent.input_requested", {"question_id": "question"})
    assert [kind for kind, _ in batches[2]] == ["attempt.text_delta", "agent.input_requested"]
    buffer.close()
    buffer.emit("attempt.text_delta", {"text": "detached late call"})
    assert len(batches) == 3


def test_large_unicode_stream_stays_readable_bounded_and_complete():
    batches = []
    buffer = StreamEventBuffer(batches.append, clock=lambda: 0)
    delta = 'Привет 😀 "\\\n' * 20
    for _ in range(1000):
        buffer.emit("attempt.text_delta", {"text": delta})
    assert batches  # Size threshold works even if a response never completes.
    buffer.close()
    bodies = [body for batch in batches for _, body in batch]
    assert "".join(body["text"] for body in bodies) == delta * 1000
    assert all(
        len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()) <= BLOCK_BYTES
        for body in bodies
    )
    assert len(batches) < 100


@pytest.mark.parametrize("with_delta", [False, True])
def test_opencode_growing_snapshots_keep_latest_text_without_quadratic_history(with_delta):
    batches = []
    buffer = StreamEventBuffer(batches.append, clock=lambda: 0)
    for index in range(1, 101):
        props = {"part": {"id": "p", "type": "text", "text": "x" * index, "time": {"start": 1}}}
        if with_delta:
            props["delta"] = "x"
        buffer.emit(
            "agent.native_event",
            {
                "harness": "opencode",
                "native_type": "message.part.updated",
                "payload": {"properties": props},
            },
        )
    buffer.close()
    assert len(batches) == 1 and len(batches[0]) == 1
    props = batches[0][0][1]["payload"]["properties"]
    assert props["part"]["text"] == "x" * 100
    if with_delta:
        assert props["delta"] == "x" * 100


def test_concurrent_callbacks_and_flushes_never_duplicate_or_lose_chunks():
    batches = []
    buffer = StreamEventBuffer(batches.append, clock=lambda: 0)

    def produce(role):
        for index in range(100):
            buffer.emit("attempt.text_delta", {"text": f"{index},", "role": role})
            if index % 17 == 0:
                buffer.flush()

    with ThreadPoolExecutor(2) as executor:
        list(executor.map(produce, ["a", "b"]))
    buffer.close()
    for role in ["a", "b"]:
        text = "".join(
            body["text"] for batch in batches for _, body in batch if body["role"] == role
        )
        assert text == "".join(f"{index}," for index in range(100))


def test_failed_persistence_is_not_hidden_or_retried_after_unknown_commit():
    calls = []

    def persist(batch):
        calls.append(batch)
        raise OSError("write failed")

    buffer = StreamEventBuffer(persist)
    buffer.emit("attempt.text_delta", {"text": "tail"})
    for action in [buffer.flush, buffer.flush_due, buffer.close]:
        with pytest.raises(OSError, match="write failed"):
            action()
    assert len(calls) == 1
