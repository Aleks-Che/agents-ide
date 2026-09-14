"""Shared helpers used by domain services: ids, hashing, JSON codec, time."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from agents_ide.errors import AppError

_NONCE_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


def utc_now() -> float:
    return datetime.now(tz=UTC).timestamp()


def utc_datetime() -> datetime:
    return datetime.now(tz=UTC)


def new_id() -> str:
    return uuid.uuid4().hex


def short_id(length: int = 16) -> str:
    return "".join(secrets.choice(_NONCE_ALPHABET) for _ in range(length))


def content_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def payload_hash(payload: Any) -> str:
    if not isinstance(payload, str):
        payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def from_json(text: str | None, default: Any) -> Any:
    if not text:
        return default
    return json.loads(text)


_NAME_PATTERN = re.compile(r"^[\w\- .]{1,120}$")


def assert_safe_name(name: str) -> None:
    if not _NAME_PATTERN.fullmatch(name):
        raise AppError("validation_error", "Недопустимое имя", 422)


def monotonic() -> float:
    return time.monotonic()
