"""Durable, bounded JSON artifacts with recursive credential redaction."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from agents_ide.domain.common import new_id, utc_now
from agents_ide.logging import redact
from agents_ide.persistence.models import ArtifactManifest

MAX_ARTIFACT_BYTES = 10 * 1024 * 1024
_KEY = re.compile(
    r"^(api[_-]?key|secret|password|access[_-]?token|refresh[_-]?token|authorization|cookie|credential|secret_reference)$",
    re.I,
)


@dataclass(frozen=True)
class ArtifactPayload:
    schema_type: str
    files: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    body: Any = None
    redaction: tuple[str, ...] = field(default_factory=tuple)
    omissions: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: "[REDACTED]" if _KEY.fullmatch(str(k)) else sanitize(v) for k, v in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        value = str(redact(value))
        value = re.sub(
            r'(?i)("(?:api[_-]?key|secret|password|access[_-]?token|authorization|cookie)"\s*:\s*)"(?:\\.|[^"\\])*"',
            r'\1"[REDACTED]"',
            value,
        )
        return re.sub(r"sk-[A-Za-z0-9_-]{16,}", "[REDACTED]", value)
    return value


def sanitize_text(text: str) -> tuple[str, tuple[str, ...]]:
    cleaned = str(sanitize(text))
    return cleaned, ("credentials",) if cleaned != text else ()


def compute_hash(content: bytes | str) -> str:
    return hashlib.sha256(
        content.encode("utf-8") if isinstance(content, str) else content
    ).hexdigest()


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def record_artifact(
    session: Session,
    run_id: str,
    payload: ArtifactPayload,
    *,
    step_execution_id: str | None = None,
    step_attempt_id: str | None = None,
    cycle_id: int | None = None,
    plan_item_ids: tuple[str, ...] = (),
    base_head_sha: str | None = None,
    current_head_sha: str | None = None,
    source_kind: str = "engine",
) -> ArtifactManifest:
    cleaned = sanitize(payload.body)
    serialised = encode(cleaned)
    redactions = set(payload.redaction)
    if cleaned != payload.body:
        redactions.add("credentials")
    original_size = len(serialised.encode("utf-8"))
    from agents_ide.operations.storage import settings_for

    truncated = (
        settings_for(session).enforce_execution_limits and original_size > MAX_ARTIFACT_BYTES
    )
    if truncated:
        # Keep a valid JSON envelope; byte limit includes escaping and metadata.
        preview = serialised.encode("utf-8")[: MAX_ARTIFACT_BYTES // 8].decode(
            "utf-8", errors="ignore"
        )
        serialised = encode(
            {"truncated": True, "original_bytes": original_size, "preview": preview}
        )
    from agents_ide.operations.storage import check_capacity
    from agents_ide.persistence.models import Run

    byte_length = len(serialised.encode("utf-8"))
    check_capacity(session, run_id, extra=byte_length, check_events=False)
    run = session.get(Run, run_id)
    if run is not None:
        run.artifact_bytes = (run.artifact_bytes or 0) + byte_length
    manifest = ArtifactManifest(
        id=new_id(),
        run_id=run_id,
        schema_type=payload.schema_type,
        body_json=serialised,
        byte_length=byte_length,
        content_hash=compute_hash(serialised),
        source_kind=source_kind,
        source_ref=None,
        step_execution_id=step_execution_id,
        step_attempt_id=step_attempt_id,
        cycle_id=cycle_id,
        plan_item_ids_json=encode(list(plan_item_ids)),
        files_json=encode(sanitize(payload.files)),
        base_head_sha=base_head_sha,
        current_head_sha=current_head_sha,
        redaction_json=encode(sorted(redactions)) if redactions else None,
        truncation_json=encode({"truncated": True, "original_bytes": original_size})
        if truncated
        else None,
        omissions_json=encode(sanitize(list(payload.omissions))) if payload.omissions else None,
        created_at=utc_now(),
    )
    session.add(manifest)
    session.flush()
    return manifest
