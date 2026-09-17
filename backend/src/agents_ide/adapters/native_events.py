"""Preserve scoped vendor envelopes; unknown kinds remain inspectable.

Protocol mapping follows sources/README.md: upstream CLI JSONL is a fixture
source, not the wire protocol of App Server or OpenCode Server.
"""

from collections.abc import Callable
from typing import Any

from agents_ide.engine.artifacts import sanitize


def archive_native(
    emit: Callable[[str, dict[str, Any]], None] | None,
    harness: str,
    kind: str,
    payload: dict[str, Any],
    session_id: str,
) -> None:
    if emit is not None:
        emit(
            "agent.native_event",
            {
                "harness": harness,
                "native_type": kind[:128],
                "session_id": session_id,
                "payload": sanitize({k: v for k, v in payload.items() if k != "_answered"}),
            },
        )
