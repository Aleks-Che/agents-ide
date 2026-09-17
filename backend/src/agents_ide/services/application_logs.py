"""Bounded access to application logs, including when the services are stopped."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from agents_ide.logging import CONTEXT_FIELDS, redact

LogRole = Literal["all", "launcher", "api", "worker", "start", "stop", "migrate", "auth"]
LogLevel = Literal["INFO", "WARNING", "ERROR"]
LogScope = Literal["current", "all"]
LOG_ROLES = ("launcher", "api", "worker", "start", "stop", "migrate", "auth")
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
MAX_FILE_BYTES = 256 * 1024


class ApplicationLogEntry(BaseModel):
    at: str
    level: str
    service: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ApplicationLogs(BaseModel):
    directory: str
    entries: list[ApplicationLogEntry]
    truncated: bool
    scope: LogScope = "all"
    current_started_at: datetime | None = None


def current_launch_started_at(data_dir: Path, fallback: datetime) -> datetime:
    """Keep the same boundary across API restarts within a managed launch."""
    launch_id = os.environ.get("AGENTS_IDE_LAUNCH_ID")
    if not launch_id:
        return fallback
    try:
        state = json.loads((data_dir / "runtime/launcher.json").read_text(encoding="utf-8"))
        if state["launch_id"] == launch_id:
            return datetime.fromtimestamp(state["launcher"]["created_at"], UTC)
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        pass
    return fallback


def read_application_logs(
    directory: Path,
    *,
    role: LogRole = "all",
    level: LogLevel = "WARNING",
    limit: int = 100,
    scope: LogScope = "all",
    current_started_at: datetime | None = None,
) -> ApplicationLogs:
    if role != "all" and role not in LOG_ROLES:
        raise ValueError("Unknown service")
    if not 1 <= limit <= 500:
        raise ValueError("Log limit must be between 1 and 500")
    if scope == "current" and current_started_at is None:
        raise ValueError("Current launch time is required")
    entries: list[ApplicationLogEntry] = []
    truncated = False
    for service in LOG_ROLES if role == "all" else (role,):
        for suffix in ("", ".1", ".2", ".3"):
            path = directory / f"{service}.jsonl{suffix}"
            if path.is_symlink():
                continue
            try:
                with path.open("rb") as stream:
                    size = stream.seek(0, 2)
                    offset = max(0, size - MAX_FILE_BYTES)
                    stream.seek(offset)
                    if offset:
                        stream.readline(MAX_FILE_BYTES)  # discard the partial first record
                        truncated = True
                    lines = (
                        stream.read(MAX_FILE_BYTES).decode("utf-8", errors="replace").splitlines()
                    )
            except FileNotFoundError:
                continue  # Files can be rotated while we read them.
            for line in lines:
                try:
                    data = json.loads(line)
                except ValueError:
                    continue  # An incomplete final record will be visible on refresh.
                if not isinstance(data, dict):
                    continue
                if not all(isinstance(data.get(key), str) for key in ("at", "level", "message")):
                    continue
                if LEVELS.get(data["level"], 0) < LEVELS[level]:
                    continue
                if scope == "current" and current_started_at is not None:
                    try:
                        at = datetime.fromisoformat(data["at"])
                        if at.tzinfo is None or at < current_started_at:
                            continue
                    except ValueError:
                        continue
                entries.append(
                    ApplicationLogEntry(
                        at=data["at"],
                        level=data["level"],
                        service=service,
                        message=redact(data["message"]),
                        details=redact(
                            {
                                key: data[key]
                                for key in (*CONTEXT_FIELDS, "exception")
                                if key in data
                            }
                        ),
                    )
                )
    entries.sort(key=lambda entry: entry.at, reverse=True)
    return ApplicationLogs(
        directory=str(directory),
        entries=entries[:limit],
        truncated=truncated or len(entries) > limit,
        scope=scope,
        current_started_at=current_started_at,
    )
