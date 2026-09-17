import json
import logging
import re
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

SENSITIVE = re.compile(r"secret|password|token|authorization|cookie|pair.?code|credential", re.I)
_known_secrets: set[str] = set()
CONTEXT_FIELDS = (
    "request_id",
    "status",
    "duration_ms",
    "worker_id",
    "event",
    "run_id",
    "service",
    "pid",
    "retries",
    "failures",
    "expected_schema",
    "actual_schema",
)


def exception_details(error: BaseException) -> list[dict[str, Any]]:
    """Keep the failure location without exception text, SQL values or frame locals."""
    chain: list[dict[str, Any]] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(chain) < 5:
        seen.add(id(current))
        frames = []
        traceback = current.__traceback__
        while traceback is not None:
            code = traceback.tb_frame.f_code
            frames.append(
                {
                    "file": Path(code.co_filename).name,
                    "line": traceback.tb_lineno,
                    "function": code.co_name,
                }
            )
            traceback = traceback.tb_next
        item: dict[str, Any] = {
            "type": type(current).__name__,
            "frames": frames[-20:],
        }
        if isinstance(current, OSError):
            item["errno"] = current.errno
        chain.append(item)
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return chain


def register_secret(value: str) -> None:
    if value:
        _known_secrets.add(value)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: "[REDACTED]" if SENSITIVE.search(str(k)) else redact(v) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        for secret in sorted(_known_secrets, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)Bearer\s+\S+", "Bearer [REDACTED]", value)
        return re.sub(
            r"(?i)(password|token|api[_-]?key|authorization|cookie|pair[_-]?code)"
            r"(\s*[:=]\s*)[^\s,;]+",
            r"\1\2[REDACTED]",
            value,
        )
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Never serialize arbitrary exception bodies, headers, URL queries or argv.
        payload: dict[str, Any] = {
            "at": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in CONTEXT_FIELDS:
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info and record.exc_info[1] is not None:
            payload["exception"] = exception_details(record.exc_info[1])
        return json.dumps(redact(payload), ensure_ascii=False)


def configure_logging(directory: Path, role: str, level: str) -> None:
    handler = RotatingFileHandler(
        directory / f"{role}.jsonl", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    for previous in root.handlers[:]:
        root.removeHandler(previous)
        previous.close()
    root.addHandler(handler)
    root.setLevel(level)
    # HTTP libraries may log credential-bearing URLs before our adapters can redact them.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
