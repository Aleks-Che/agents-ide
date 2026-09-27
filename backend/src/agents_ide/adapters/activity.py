"""Bounded activity signals without storing tool bodies or reasoning."""

import time
from collections.abc import Callable
from typing import Any


class ActivityPulse:
    def __init__(self, emit: Callable[[str, dict[str, Any]], None] | None) -> None:
        self.emit = emit
        self.last_sent = float("-inf")

    def __call__(self) -> None:
        now = time.monotonic()
        if self.emit and now - self.last_sent >= 5:
            self.last_sent = now
            self.emit("attempt.progress", {"activity": True})


def is_progress(kind: str, payload: dict[str, Any]) -> bool:
    # Repeated busy/retry notices prove liveness, not progress.
    return not (
        kind == "attempt.progress"
        and payload.get("native_type") in {"session.status", "session.idle"}
    )
