"""Persist ordered intervals even when the operating system clock moves backwards."""

from datetime import datetime
from typing import Any


def close_interval(intervals: list[dict[str, Any]], now: datetime) -> tuple[str, str]:
    quality = "observed"
    if intervals:
        last = intervals[-1]
        boundary = datetime.fromisoformat(last["ended_at"] or last["started_at"])
        if now < boundary:
            now = boundary
            quality = "unknown"
            last["quality"] = "unknown"
        if last["ended_at"] is None:
            last["ended_at"] = now.isoformat()
    return now.isoformat(), quality


def interval_view(intervals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Historical intervals written before this guard remain readable. Do not
    # invent a measured duration or rewrite the underlying historical record.
    for interval in intervals:
        end = interval.get("ended_at")
        if end and datetime.fromisoformat(end) < datetime.fromisoformat(interval["started_at"]):
            interval["ended_at"] = interval["started_at"]
            interval["quality"] = "unknown"
    return intervals
