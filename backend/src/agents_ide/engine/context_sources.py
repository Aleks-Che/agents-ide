"""CollectContext sources, bounded packages and missing-evidence validation.

All paths are relative to the reserved workspace and are re-checked after
resolution; links that leave the workspace are omissions, never reads. The
full package is recorded as an artifact while the execution stores only a
compact summary, so a large diff cannot inflate the event stream.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agents_ide.errors import AppError
from agents_ide.persistence.models import ArtifactManifest

DEFAULT_MAX_FILES = 100
DEFAULT_MAX_FILE_BYTES = 256 * 1024
DEFAULT_MAX_TOTAL_BYTES = 2 * 1024 * 1024
DEFAULT_SUMMARY_BYTES = 64 * 1024
PROTECTED_PARTS = {".git", ".env", "secrets"}
CONTEXT_SCHEMA = "context_package"


@dataclass
class ContextCollection:
    package: dict[str, Any]
    files: list[dict[str, Any]] = field(default_factory=list)
    omissions: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)


def _omission(kind: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "reason": reason, **extra}


def _safe_relative(value: str) -> bool:
    if not value or "\x00" in value:
        return False
    path = PureWindowsPath(value)
    if path.drive or path.root or ".." in path.parts or ":" in value:
        return False
    return not any(
        part.lower() in PROTECTED_PARTS | {".ssh", ".aws", "credentials", "id_rsa", "id_ed25519"}
        or part.lower().startswith(".env.")
        or part.lower().endswith((".pem", ".key", ".p12", ".pfx"))
        for part in path.parts
    )


def _inside(path: Path, workspace: Path) -> bool:
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return False
    return resolved.is_relative_to(workspace.resolve())


def _git_output(
    workspace: Path,
    args: list[str],
    *,
    timeout: float = 20,
    launcher: Any = None,
    stop_event: Any = None,
    deadline_at: float | None = None,
    cap: int = DEFAULT_MAX_TOTAL_BYTES,
) -> str | None:
    from agents_ide.engine.commands import CommandSpec, StartedProcess, execute_commands
    from agents_ide.worker.processes import ProcessGroup

    def local_launch(spec: Any, cwd: Path, env: dict[str, str], argv: list[str]) -> StartedProcess:
        group = ProcessGroup()
        return StartedProcess(group.popen_stdio(argv, cwd, env, stdin=subprocess.DEVNULL), group)

    result = execute_commands(
        [
            CommandSpec(
                id="evidence_git",
                program="git",
                args=("-c", "core.fsmonitor=false", "-c", "core.quotePath=false", *args),
                env={"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"},
                retry_safety="safe",
                timeout_seconds=timeout,
                max_output_bytes=cap,
            )
        ],
        workspace=workspace,
        launcher=launcher or local_launch,
        stop_event=stop_event,
        deadline_at=deadline_at,
    )
    reports = json.loads(result.raw_text or "{}").get("commands", [])
    if not reports or reports[0]["status"] != "completed" or reports[0]["output_truncated"]:
        return None
    return str(reports[0]["stdout"])


def _read_file(
    workspace: Path, relative: str, max_bytes: int
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    from agents_ide.engine.artifacts import sanitize_text
    from agents_ide.security.workspace_read import read_workspace_file

    if not _safe_relative(relative):
        return None, _omission("file", "protected_or_invalid_path", path=relative)
    try:
        raw = read_workspace_file(workspace, str(Path(*PureWindowsPath(relative).parts)), max_bytes)
    except (OSError, ValueError) as exc:
        return None, _omission(
            "file", str(exc) if isinstance(exc, ValueError) else "unavailable", path=relative
        )
    if b"\x00" in raw:
        return None, _omission("file", "binary", path=relative, bytes=len(raw))
    cleaned, redactions = sanitize_text(raw.decode("utf-8", errors="replace"))
    return {
        "path": relative.replace("\\", "/"),
        "kind": "file",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "content": cleaned,
        "redacted": bool(redactions),
    }, None


def workspace_hash(workspace: Path) -> str | None:
    """Conservative freshness proof; a bounded/unreadable tree has no proof."""
    import os

    from agents_ide.security.workspace_read import read_workspace_file

    digest = hashlib.sha256()
    count = total = 0
    try:
        for directory, dirs, names in os.walk(workspace, followlinks=False):
            dirs[:] = sorted(
                d for d in dirs if d not in {".git", "__pycache__", ".pytest_cache", ".ruff_cache"}
            )
            for name in sorted(names):
                path = Path(directory) / name
                relative = path.relative_to(workspace).as_posix()
                if not _safe_relative(relative):
                    continue
                count += 1
                if count > 10000 or total > 64 * 1024 * 1024:
                    return None
                raw = read_workspace_file(workspace, relative, 64 * 1024 * 1024 - total)
                total += len(raw)
                digest.update(relative.encode())
                digest.update(b"\0")
                digest.update(hashlib.sha256(raw).digest())
        return digest.hexdigest()
    except (OSError, ValueError):
        return None


def artifact_body(row: ArtifactManifest) -> dict[str, Any]:
    try:
        body = json.loads(row.body_json or "{}")
        if isinstance(body, dict) and isinstance(body.get("raw_text"), str):
            body = json.loads(body["raw_text"])
        return body if isinstance(body, dict) else {}
    except ValueError:
        return {}


def _source_list(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    sources = config.get("sources")
    result = [dict(item) for item in sources] if isinstance(sources, list) else []
    context_paths = config.get("context_paths")
    if isinstance(context_paths, list):
        for path in context_paths:
            if isinstance(path, str):
                kind = "glob" if any(char in path for char in "*?[") else "file"
                result.append({"kind": kind, "path": path})
    return result


def _excluded(relative: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(relative.replace("\\", "/"), pattern) for pattern in patterns)


def collect_context(
    config: Mapping[str, Any],
    *,
    workspace: Path,
    session: Session,
    run_id: str,
    base_head_sha: str | None,
    cycle_id: int | None = None,
    scope: str | None = None,
    launcher: Any = None,
    stop_event: Any = None,
    deadline_at: float | None = None,
) -> ContextCollection:
    from agents_ide.engine.artifacts import encode, sanitize
    from agents_ide.persistence.models import StepExecution

    max_files = min(int(config.get("max_files", DEFAULT_MAX_FILES)), DEFAULT_MAX_FILES)
    max_file = min(
        int(config.get("max_file_bytes", DEFAULT_MAX_FILE_BYTES)), DEFAULT_MAX_FILE_BYTES
    )
    cap = min(int(config.get("max_total_bytes", DEFAULT_MAX_TOTAL_BYTES)), DEFAULT_MAX_TOTAL_BYTES)
    summary_cap = min(
        int(config.get("summary_max_bytes", DEFAULT_SUMMARY_BYTES)), DEFAULT_SUMMARY_BYTES
    )
    strategy = str(config.get("strategy", "truncate"))

    def git(args: list[str], limit: int = DEFAULT_MAX_TOTAL_BYTES) -> str | None:
        return _git_output(
            workspace,
            args,
            launcher=launcher,
            stop_event=stop_event,
            deadline_at=deadline_at,
            cap=limit,
        )

    tracked_raw = git(["ls-files", "-z"])
    tracked = set(tracked_raw.split("\0")) if tracked_raw is not None else None
    before_hash = workspace_hash(workspace)
    head = git(["rev-parse", "--verify", "HEAD"], 1024)
    package: dict[str, Any] = {
        "kind": CONTEXT_SCHEMA,
        "strategy": strategy,
        "files": [],
        "omissions": [],
        "truncated": False,
        "base_head_sha": base_head_sha,
        "current_head_sha": head.strip() if head else None,
        "workspace_hash": before_hash,
    }
    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    omission_count = 0

    def omit(problem: dict[str, Any]) -> None:
        nonlocal omission_count
        omission_count += 1
        if len(package["omissions"]) < 100:
            package["omissions"].append(sanitize(problem))
        else:
            package["truncated"] = True

    def add(record: dict[str, Any], limit: int | None = None) -> None:
        record = sanitize(record)
        if strategy == "summary":
            record["content"] = None
            record["content_omitted"] = True
        content = record.get("content")
        # Each source and the complete JSON envelope share the byte budget.
        source_cap = min(cap, limit if limit is not None else cap)
        if len(encode(content).encode()) > source_cap:
            record["content"] = None
            content = None
            record["content_omitted"] = True
            package["truncated"] = True
            omit(_omission(record["kind"], "source_limit", path=record.get("path")))
        package["files"].append(record)
        if len(encode(package).encode()) > cap:
            package["truncated"] = True
            if strategy == "truncate" and isinstance(content, str):
                low, high = 0, len(content)
                while low < high:
                    middle = (low + high + 1) // 2
                    record["content"] = content[:middle]
                    if len(encode(package).encode()) <= max(0, cap - 150):
                        low = middle
                    else:
                        high = middle - 1
                record["content"] = content[:low]
                record["content_truncated"] = True
            else:
                record["content"] = None
                record["content_omitted"] = True
            omit(_omission(record["kind"], "total_limit", path=record.get("path")))

    def add_file(source: Mapping[str, Any]) -> None:
        path = str(source.get("path", "")).replace("\\", "/")
        if path in seen:
            return
        seen.add(path)
        if len(files) >= max_files:
            omit(_omission("file", "file_limit", path=path))
            return
        if (
            tracked is not None
            and not source.get("include_untracked", config.get("include_untracked", False))
            and path not in tracked
        ):
            omit(_omission("file", "untracked_excluded", path=path))
            return
        record, problem = _read_file(
            workspace, path, min(max_file, int(source.get("max_bytes", max_file)))
        )
        if problem:
            omit(problem)
        elif record:
            files.append({key: value for key, value in record.items() if key != "content"})
            add(record)

    for source in _source_list(config):
        if stop_event is not None and stop_event.is_set():
            raise AppError("interrupted", "Context collection stopped", 409)
        kind = source.get("kind")
        if kind == "file":
            add_file(source)
        elif kind == "glob":
            pattern = str(source.get("path", "")).replace("\\", "/")
            if not _safe_relative(pattern):
                omit(_omission("glob", "protected_or_invalid_path", path=pattern))
                continue
            import os

            scanned = 0
            for directory, dirs, names in os.walk(workspace, followlinks=False):
                dirs[:] = sorted(
                    d
                    for d in dirs
                    if _safe_relative((Path(directory) / d).relative_to(workspace).as_posix())
                    and not (Path(directory) / d).is_junction()
                    and not (Path(directory) / d).is_symlink()
                )
                for name in sorted(names):
                    scanned += 1
                    if scanned > 10000:
                        break
                    relative = (Path(directory) / name).relative_to(workspace).as_posix()
                    if _match_path(relative, pattern) and not _excluded(
                        relative, list(source.get("exclude", []))
                    ):
                        add_file({**source, "path": relative})
                if scanned > 10000:
                    omit(_omission("glob", "scan_limit"))
                    break
        elif kind == "diff":
            baseline = base_head_sha or "HEAD"
            changed = git(
                ["diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", baseline, "--"]
            )
            if changed is None:
                omit(_omission("diff", "baseline_unavailable"))
                continue
            paths = [path for path in changed.split("\0") if path]
            for path in paths[:100]:
                if not _safe_relative(path):
                    omit(_omission("diff", "protected_or_invalid_path", path=path))
                    continue
                diff = git(
                    ["diff", "--no-ext-diff", "--no-textconv", "--no-color", baseline, "--", path]
                )
                if diff is None:
                    omit(_omission("diff", "unavailable_or_too_large", path=path))
                else:
                    add(
                        {
                            "kind": "diff",
                            "path": path,
                            "content": diff,
                            "sha256": hashlib.sha256(diff.encode()).hexdigest(),
                        },
                        int(source.get("max_bytes", cap)),
                    )
            if len(paths) > 100:
                omit(_omission("diff", "file_limit"))
        elif kind in {"artifact", "command_report"}:
            rows = session.scalars(
                select(ArtifactManifest)
                .where(
                    ArtifactManifest.run_id == run_id,
                    *([ArtifactManifest.cycle_id == cycle_id] if cycle_id is not None else []),
                    *(
                        [ArtifactManifest.id == source.get("artifact_id")]
                        if kind == "artifact"
                        else [ArtifactManifest.schema_type == "command_ledger"]
                    ),
                )
                .order_by(ArtifactManifest.created_at.desc())
                .limit(500)
            )
            found = False
            for row in rows:
                execution = (
                    session.get(StepExecution, row.step_execution_id)
                    if row.step_execution_id
                    else None
                )
                if scope is not None and (execution is None or execution.scope != scope):
                    continue
                body = artifact_body(row)
                if (
                    row.truncation_json
                    or body.get("truncated")
                    or body.get("workspace_hash") != before_hash
                    or before_hash is None
                ):
                    continue
                content: Any = body
                if kind == "command_report":
                    content = next(
                        (
                            item
                            for item in body.get("commands", [])
                            if item.get("id") == source.get("command_id")
                        ),
                        None,
                    )
                    if content is None:
                        continue
                add(
                    {
                        "kind": kind,
                        "path": f"{kind}:{row.id}",
                        "artifact_id": row.id,
                        "content_hash": row.content_hash,
                        "content": content,
                    },
                    int(source.get("max_bytes", cap)),
                )
                found = True
                break
            if not found:
                omit(
                    _omission(
                        str(kind),
                        "missing_or_stale",
                        artifact_id=source.get("artifact_id"),
                        command_id=source.get("command_id"),
                    )
                )
        else:
            omit(_omission("source", "unsupported_kind"))
    if workspace_hash(workspace) != before_hash or before_hash is None:
        package["workspace_hash"] = None
        omit(_omission("workspace", "unstable_or_unverified"))
    package["omission_count"] = omission_count
    # Metadata itself is bounded, even for thousands of omitted paths.
    while len(encode(package).encode()) > cap and (package["files"] or package["omissions"]):
        package["truncated"] = True
        (package["omissions"] if package["omissions"] else package["files"]).pop()
    summary = {key: value for key, value in package.items() if key != "files"}
    summary["files"] = files.copy()
    summary["omissions"] = list(package["omissions"])
    while len(encode(summary).encode()) > summary_cap and (
        summary["files"] or summary["omissions"]
    ):
        summary["summary_truncated"] = True
        (summary["omissions"] if summary["omissions"] else summary["files"]).pop()
    if len(encode(summary).encode()) > summary_cap:
        summary = {
            "kind": CONTEXT_SCHEMA,
            "summary_truncated": True,
            "omission_count": omission_count,
        }
    return ContextCollection(package, files, package["omissions"], summary)


def _match_path(path: str, pattern: str) -> bool:
    # Same matching rules for collection and verifier requests, including root **/.
    parts, patterns = path.lower().split("/"), pattern.lower().split("/")

    def match(i: int, j: int) -> bool:
        if j == len(patterns):
            return i == len(parts)
        if patterns[j] == "**":
            return match(i, j + 1) or (i < len(parts) and match(i + 1, j))
        return i < len(parts) and fnmatch.fnmatchcase(parts[i], patterns[j]) and match(i + 1, j + 1)

    return match(0, 0)


def build_path_matcher(config: Mapping[str, Any]) -> Any:
    """Return a predicate deciding whether a missing-evidence path is configured."""

    sources = _source_list(config)

    def allowed(path: str) -> bool:
        normalized = path.replace("\\", "/")
        for source in sources:
            kind = source.get("kind")
            pattern = source.get("path")
            if not isinstance(pattern, str):
                continue
            candidate = pattern.replace("\\", "/")
            if kind == "file" and normalized == candidate:
                return True
            if (
                kind == "glob"
                and _match_path(normalized, candidate)
                and not _excluded(normalized, list(source.get("exclude", [])))
            ):
                return True
        return False

    return allowed


def resolve_requests(
    config: Mapping[str, Any],
    *,
    missing: Iterable[Mapping[str, Any]],
    artifact_exists: Any,
    commands: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate verifier ``missing_evidence`` without running anything."""

    matcher = build_path_matcher(config)
    allowed: list[dict[str, Any]] = []
    denied: list[dict[str, Any]] = []
    command_ids: list[str] = []
    for item in missing:
        if not isinstance(item, Mapping):
            denied.append({"reason": "invalid_item"})
            continue
        kind = item.get("kind")
        reason = item.get("reason")
        if (
            kind not in {"file", "artifact", "command_report"}
            or not isinstance(reason, str)
            or not reason
        ):
            denied.append({"reason": "invalid_item", "kind": kind})
            continue
        if kind == "file":
            path = item.get("path")
            if not isinstance(path, str) or not _safe_relative(path):
                denied.append({"kind": kind, "path": path, "reason": "protected_or_invalid_path"})
            elif not matcher(path):
                denied.append({"kind": kind, "path": path, "reason": "not_configured"})
            else:
                allowed.append({"kind": kind, "path": path, "reason": reason})
        elif kind == "artifact":
            artifact_id = item.get("artifact_id")
            configured = any(
                source.get("kind") == "artifact" and source.get("artifact_id") == artifact_id
                for source in _source_list(config)
            )
            if (
                not isinstance(artifact_id, str)
                or not configured
                or not artifact_exists(artifact_id)
            ):
                denied.append(
                    {"kind": kind, "artifact_id": artifact_id, "reason": "unknown_artifact"}
                )
            else:
                allowed.append({"kind": kind, "artifact_id": artifact_id, "reason": reason})
        else:
            command_id = item.get("command_id")
            spec = commands.get(command_id) if isinstance(command_id, str) else None
            if spec is None:
                denied.append({"kind": kind, "command_id": command_id, "reason": "unknown_command"})
            elif spec.retry_safety != "safe":
                denied.append({"kind": kind, "command_id": command_id, "reason": "unsafe_command"})
            else:
                assert isinstance(command_id, str)
                allowed.append({"kind": kind, "command_id": command_id, "reason": reason})
                if command_id not in command_ids:
                    command_ids.append(command_id)
    return {
        "kind": "resolve_requests",
        "allowed_requests": allowed,
        "denied_requests": denied,
        "command_ids": command_ids,
    }


def ensure_workspace_readable(workspace: Path) -> None:
    if not workspace.is_dir():
        raise AppError("path_unavailable", "Рабочий каталог недоступен", 404)
