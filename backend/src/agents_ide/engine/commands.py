"""Selected command lists with owned process trees, bounded output and a ledger.

The executor is deliberately independent from the graph AST: the runner
resolves the configured list (including typed ``input.verification_commands``
references), then calls :func:`execute_commands`. Every command result is
durable; resume reuses completed unsafe commands instead of replaying effects.
"""

from __future__ import annotations

import math
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agents_ide.adapters.base import AdapterError, ExternalOutcome, LLMResult
from agents_ide.errors import AppError

if TYPE_CHECKING:
    from agents_ide.worker.processes import ProcessGroup, ProcessRegistryEntry

DEFAULT_MAX_OUTPUT_BYTES = 10 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 300.0
REPORT_SCHEMA = "command_report"

CommandLauncher = Callable[["CommandSpec", Path, "dict[str, str]", "list[str]"], "StartedProcess"]

_WINDOWS_ENV_KEYS = (
    "SystemRoot",
    "SystemDrive",
    "windir",
    "COMSPEC",
    "PATHEXT",
    "PATH",
    "TEMP",
    "TMP",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "USERPROFILE",
)
_POSIX_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TZ")
_SENSITIVE_ENV = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


@dataclass(frozen=True)
class CommandSpec:
    id: str
    program: str
    args: tuple[str, ...]
    cwd: str = "."
    env: Mapping[str, str] = field(default_factory=dict)
    required: bool = True
    success_exit_codes: tuple[int, ...] = (0,)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    retry_safety: str = "unknown"


@dataclass(frozen=True)
class CommandReport:
    id: str
    status: str
    exit_code: int | None
    duration_seconds: float
    stdout: str
    stderr: str
    output_truncated: bool
    dropped_bytes: int
    required: bool
    retry_safety: str
    reason: str | None = None
    reused: bool = False

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "exit_code": self.exit_code,
            "duration_seconds": round(self.duration_seconds, 3),
            "output_truncated": self.output_truncated,
            "dropped_bytes": self.dropped_bytes,
            "required": self.required,
            "retry_safety": self.retry_safety,
            "reason": self.reason,
            "reused": self.reused,
        }

    def full(self) -> dict[str, Any]:
        return {**self.summary(), "stdout": self.stdout, "stderr": self.stderr}


@dataclass
class StartedProcess:
    popen: subprocess.Popen[str]
    group: ProcessGroup | None = None
    entry: ProcessRegistryEntry | None = None

    def kill(self) -> None:
        if self.group is not None:
            self.group.close()
            return
        _kill_tree(self.popen.pid)


def _kill_tree(pid: int) -> None:
    import contextlib

    import psutil

    try:
        process = psutil.Process(pid)
        children = process.children(recursive=True)
    except psutil.Error:
        return
    for child in children:
        with contextlib.suppress(psutil.Error):
            child.kill()
    with contextlib.suppress(psutil.Error):
        process.kill()


def command_environment(explicit: Mapping[str, str] | None = None) -> dict[str, str]:
    """Minimal OS environment plus explicit values; host credentials never leak."""

    import os
    import sys

    keys = _WINDOWS_ENV_KEYS if sys.platform == "win32" else _POSIX_ENV_KEYS
    environment = {key: os.environ[key] for key in keys if key in os.environ}
    for key, value in (explicit or {}).items():
        upper = key.upper()
        if any(marker in upper for marker in _SENSITIVE_ENV):
            raise AppError(
                "configuration_invalid", f"Секретный ключ окружения запрещён: {key}", 422
            )
        environment[key] = value
    return environment


def parse_command_list(raw: Any, inputs: Mapping[str, Any] | None = None) -> list[CommandSpec]:
    """Resolve a command list, including a typed ``input.<name>`` reference."""

    if isinstance(raw, Mapping):
        ref = raw.get("ref")
        if not isinstance(ref, str) or not ref.startswith("input."):
            raise AppError("configuration_invalid", "Неверная ссылка на список команд", 422)
        current: Any = {"input": dict(inputs or {})}
        for part in ref.split("."):
            if not isinstance(current, Mapping) or part not in current:
                raise AppError("configuration_invalid", "Список команд не найден во входах", 422)
            current = current[part]
        raw = current
    if not isinstance(raw, list) or not raw:
        raise AppError("configuration_invalid", "Список команд пуст", 422)
    if len(raw) > 50:
        raise AppError("configuration_invalid", "Слишком много команд", 422)
    specs: list[CommandSpec] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise AppError("configuration_invalid", "Неверное описание команды", 422)
        spec = _parse_command(item)
        if spec.id in seen:
            raise AppError("configuration_invalid", "Дубликат ID команды", 422)
        seen.add(spec.id)
        specs.append(spec)
    return specs


def _parse_command(item: Mapping[str, Any]) -> CommandSpec:
    def _text(value: Any, name: str) -> str:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise AppError("configuration_invalid", f"Неверное поле команды: {name}", 422)
        return value

    command_id = _text(item.get("id"), "id")
    if not command_id or any(char.isspace() for char in command_id):
        raise AppError("configuration_invalid", "Неверный ID команды", 422)
    program = _text(item.get("program"), "program")
    args_raw = item.get("args", [])
    if not isinstance(args_raw, list) or any(
        not isinstance(arg, str) or "\x00" in arg for arg in args_raw
    ):
        raise AppError("configuration_invalid", "Неверные аргументы команды", 422)
    if len(args_raw) > 64:
        raise AppError("configuration_invalid", "Слишком много аргументов", 422)
    env_raw = item.get("env", {})
    if not isinstance(env_raw, Mapping) or any(
        not isinstance(key, str)
        or not key
        or "=" in key
        or not isinstance(value, str)
        or "\x00" in key
        or "\x00" in value
        for key, value in env_raw.items()
    ):
        raise AppError("configuration_invalid", "Неверное окружение команды", 422)
    codes_raw = item.get("success_exit_codes", [0])
    if (
        not isinstance(codes_raw, list)
        or not codes_raw
        or any(not isinstance(code, int) or isinstance(code, bool) for code in codes_raw)
    ):
        raise AppError("configuration_invalid", "Неверные success_exit_codes", 422)
    timeout = item.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(timeout)
        or not 0 < timeout <= 86400
    ):
        raise AppError("configuration_invalid", "Неверный timeout команды", 422)
    cap = item.get("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES)
    if (
        not isinstance(cap, int)
        or isinstance(cap, bool)
        or not 1024 <= cap <= DEFAULT_MAX_OUTPUT_BYTES
    ):
        raise AppError("configuration_invalid", "Неверный лимит вывода команды", 422)
    retry_safety = item.get("retry_safety", "unknown")
    if retry_safety not in {"safe", "unsafe", "unknown"}:
        raise AppError("configuration_invalid", "Неверный retry_safety команды", 422)
    failure_policy = item.get("failure_policy")
    if failure_policy is not None and failure_policy not in {"collect_all", "stop_on_failure"}:
        raise AppError("configuration_invalid", "Неверный failure_policy команды", 422)
    cwd = item.get("cwd", ".")
    if not isinstance(cwd, str) or "\x00" in cwd:
        raise AppError("configuration_invalid", "Неверный cwd команды", 422)
    if not isinstance(item.get("required", True), bool):
        raise AppError("configuration_invalid", "Неверный required команды", 422)
    from agents_ide.domain.graph_ast import ASTError
    from agents_ide.domain.graph_validation import check_relative_path
    from agents_ide.engine.artifacts import sanitize

    try:
        check_relative_path(cwd)
    except ASTError:
        raise AppError("configuration_invalid", "Неверный cwd команды", 422) from None
    command_environment(env_raw)
    if sanitize([program, args_raw, list(env_raw.values())]) != [
        program,
        args_raw,
        list(env_raw.values()),
    ]:
        raise AppError("configuration_invalid", "Секреты в команде запрещены", 422)
    return CommandSpec(
        id=command_id,
        program=program,
        args=tuple(args_raw),
        cwd=cwd,
        env=dict(env_raw),
        required=bool(item.get("required", True)),
        success_exit_codes=tuple(int(code) for code in codes_raw),
        timeout_seconds=float(timeout),
        max_output_bytes=int(cap),
        retry_safety=str(retry_safety),
    )


def resolve_program(program: str, workspace: Path, path_value: str | None) -> str:
    if "\x00" in program:
        raise AppError("configuration_invalid", "Неверная программа", 422)
    candidate = Path(program)
    if candidate.is_absolute():
        if not candidate.is_file():
            raise AppError("configuration_invalid", f"Программа не найдена: {program}", 422)
        return str(candidate)
    if candidate.parent != Path("."):
        target = (workspace / candidate).resolve()
        if not target.is_relative_to(workspace.resolve()) or not target.is_file():
            raise AppError("configuration_invalid", f"Программа не найдена: {program}", 422)
        return str(target)
    resolved = shutil.which(program, path=path_value)
    if resolved is None:
        raise AppError("configuration_invalid", f"Программа не найдена: {program}", 422)
    return resolved


def resolve_cwd(workspace: Path, cwd: str) -> Path:
    target = (workspace / cwd).resolve()
    if not target.is_relative_to(workspace.resolve()) or not target.is_dir():
        raise AppError("configuration_invalid", f"Рабочий каталог команды недоступен: {cwd}", 422)
    return target


def _run_single(
    spec: CommandSpec,
    *,
    workspace: Path,
    environment: Mapping[str, str],
    launcher: CommandLauncher,
    stop_event: threading.Event | None,
    deadline_at: float | None,
) -> CommandReport:
    started = time.monotonic()
    cwd = resolve_cwd(workspace, spec.cwd)
    env = {**environment, **command_environment(spec.env)}
    try:
        program = resolve_program(spec.program, workspace, env.get("PATH"))
    except AppError as exc:
        return CommandReport(
            spec.id,
            "launch_error",
            None,
            time.monotonic() - started,
            "",
            "",
            False,
            0,
            spec.required,
            spec.retry_safety,
            reason=exc.code,
        )
    argv = [program, *spec.args]
    try:
        from agents_ide.security.workspace_read import hold_command_directory

        with hold_command_directory(workspace, cwd):
            started_process = launcher(spec, cwd, env, argv)
    except (OSError, AppError, ValueError) as exc:
        return CommandReport(
            spec.id,
            "launch_error",
            None,
            time.monotonic() - started,
            "",
            "",
            False,
            0,
            spec.required,
            spec.retry_safety,
            reason=getattr(exc, "code", type(exc).__name__),
        )
    child = started_process.popen
    import psutil

    sinks = [bytearray(), bytearray()]
    counts = [0, 0]  # retained and dropped bytes across both streams
    lock = threading.Lock()
    readers: list[threading.Thread] = []

    def drain(stream: Any, sink: bytearray) -> None:
        if stream is None:
            return
        source = getattr(stream, "buffer", stream)
        try:
            while chunk := source.read1(65536):
                with lock:
                    keep = (
                        len(chunk)
                        if deadline_at == float("inf")
                        else min(len(chunk), max(0, spec.max_output_bytes - counts[0]))
                    )
                    sink.extend(chunk[:keep])
                    counts[0] += keep
                    counts[1] += len(chunk) - keep
        except (OSError, ValueError):
            pass
        finally:
            stream.close()

    for stream, sink in zip((child.stdout, child.stderr), sinks, strict=True):
        reader = threading.Thread(target=drain, args=(stream, sink), daemon=True)
        reader.start()
        readers.append(reader)
    deadline = (
        deadline_at
        if deadline_at == float("inf")
        else min(started + spec.timeout_seconds, deadline_at or float("inf"))
    )
    status, reason = "completed", None
    tree: dict[int, psutil.Process] = {}
    try:
        while True:
            try:
                members = (
                    started_process.group.members()
                    if started_process.group
                    else [psutil.Process(child.pid)]
                )
                tree.update({member.pid: member for member in members})
                alive = [
                    proc
                    for proc in tree.values()
                    if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
                ]
            except psutil.NoSuchProcess:
                alive = [] if child.poll() is not None else list(tree.values())
            except psutil.Error:
                status, reason = "unknown", "tree_unverified"
                break
            if child.poll() is not None and not alive:
                break
            if stop_event is not None and stop_event.is_set():
                status, reason = "interrupted", "stop_requested"
                break
            if time.monotonic() >= deadline:
                status, reason = "timeout", "timeout"
                break
            time.sleep(0.05)
    finally:
        # The command owns its entire tree, including children outliving its root.
        started_process.kill()
    exit_code: int | None
    try:
        exit_code = child.wait(timeout=5)
        _, alive = psutil.wait_procs(list(tree.values()), timeout=5)
        if alive:
            status, reason = "unknown", "not_responding"
    except (subprocess.TimeoutExpired, psutil.Error):
        exit_code = child.poll()
        status, reason = "unknown", "not_responding"
    for reader in readers:
        reader.join(timeout=2)
    if any(reader.is_alive() for reader in readers):
        status, reason = "unknown", "output_not_closed"
    if status == "completed":
        status = "completed" if exit_code in spec.success_exit_codes else "failed"
    return CommandReport(
        spec.id,
        status,
        exit_code,
        time.monotonic() - started,
        sinks[0].decode("utf-8", errors="replace"),
        sinks[1].decode("utf-8", errors="replace"),
        counts[1] > 0,
        counts[1],
        spec.required,
        spec.retry_safety,
        reason=reason,
    )


def previous_reports(reports: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        report["id"]: report
        for report in reports
        if isinstance(report, dict) and isinstance(report.get("id"), str)
    }


def execute_commands(
    commands: list[CommandSpec],
    *,
    workspace: Path,
    launcher: CommandLauncher,
    failure_policy: str = "collect_all",
    completed: Mapping[str, Mapping[str, Any]] | None = None,
    stop_event: threading.Event | None = None,
    deadline_at: float | None = None,
    on_report: Callable[[CommandReport], None] | None = None,
    on_start: Callable[[CommandSpec], None] | None = None,
) -> LLMResult:
    """Run the list and return a normalized envelope.

    ``completed`` carries the previous attempt's reports; an unsafe completed
    or unknown command is never replayed automatically.
    """

    environment = command_environment()
    reports: list[CommandReport] = []
    technical_unknown = False
    launch_configuration_error = False
    interrupted = False

    for spec in commands:
        if stop_event is not None and stop_event.is_set():
            interrupted = True
            break
        previous = (completed or {}).get(spec.id)
        if (
            previous is not None
            and previous.get("status") not in {"skipped", "launch_error"}
            and (spec.retry_safety != "safe" or previous.get("status") in {"unknown", "prepared"})
        ):
            previous_status = str(previous.get("status", "unknown"))
            if previous_status in {"prepared", "interrupted", "timeout"}:
                previous_status = "unknown"
            report = CommandReport(
                spec.id,
                previous_status,
                previous.get("exit_code"),
                float(previous.get("duration_seconds") or 0),
                str(previous.get("stdout") or ""),
                str(previous.get("stderr") or ""),
                bool(previous.get("output_truncated")),
                int(previous.get("dropped_bytes") or 0),
                spec.required,
                spec.retry_safety,
                reason="not_replayed",
                reused=True,
            )
            reports.append(report)
            if previous_status == "unknown":
                technical_unknown = True
                break
            if previous_status != "completed" and failure_policy == "stop_on_failure":
                break
            continue
        if deadline_at is not None and time.monotonic() >= deadline_at:
            interrupted = True
            break
        if on_start is not None:
            on_start(spec)
        report = _run_single(
            spec,
            workspace=workspace,
            environment=environment,
            launcher=launcher,
            stop_event=stop_event,
            deadline_at=deadline_at,
        )
        reports.append(report)
        if on_report is not None:
            on_report(report)
        if report.status == "launch_error":
            if report.reason == "dispatch_blocked":
                interrupted = True
            else:
                launch_configuration_error = True
        elif report.status == "unknown" or (
            report.status in {"timeout", "interrupted"} and report.retry_safety != "safe"
        ):
            technical_unknown = True
        if report.status == "interrupted":
            interrupted = True
        if technical_unknown or interrupted:
            break
        if report.status != "completed" and failure_policy == "stop_on_failure":
            break
    selected = {report.id for report in reports}
    if failure_policy == "stop_on_failure":
        for spec in commands:
            if spec.id not in selected:
                reports.append(
                    CommandReport(
                        spec.id,
                        "skipped",
                        None,
                        0.0,
                        "",
                        "",
                        False,
                        0,
                        spec.required,
                        spec.retry_safety,
                        reason="stop_on_failure",
                    )
                )
    started_reports = [
        report for report in reports if report.status not in {"skipped", "launch_error"}
    ]
    if interrupted and any(
        report.retry_safety != "safe" and report.status == "interrupted"
        for report in started_reports
    ):
        technical_unknown = True

    required_failed = [
        report for report in reports if report.required and report.status != "completed"
    ]
    required_completed = [
        report for report in reports if report.required and report.status == "completed"
    ]
    full = [report.full() for report in reports]
    summary = [report.summary() for report in reports]
    verdict: str | None
    if required_failed:
        verdict = "failed"
    elif required_completed:
        verdict = (
            "inconclusive"
            if any(report.output_truncated for report in required_completed)
            else "passed"
        )
    else:
        verdict = "inconclusive" if reports else None
    if technical_unknown:
        outcome = ExternalOutcome.UNKNOWN
        decision = None
    elif interrupted:
        outcome = ExternalOutcome.RETRYABLE_FAILURE
        decision = None
    elif launch_configuration_error:
        outcome = ExternalOutcome.CONFIRMED_FAILURE
        decision = None
    else:
        outcome = ExternalOutcome.SUCCEEDED
        decision = verdict
    validated = {
        "kind": "command_report",
        "verdict": verdict,
        "commands": summary,
        "required_failed": [report.id for report in required_failed],
    }
    raw = _encode_reports(full)
    return LLMResult(
        outcome,
        raw,
        validated,
        decision,
        error=AdapterError("interrupted", "Command list stopped", "safe")
        if interrupted and not technical_unknown
        else AdapterError("configuration_invalid", "Команда не может быть запущена", "safe")
        if launch_configuration_error
        else AdapterError("command_outcome_unknown", "Часть команд не подтвердила исход", "unknown")
        if technical_unknown
        else None,
        no_effect=(launch_configuration_error and not started_reports)
        or (
            interrupted
            and not technical_unknown
            and all(
                r.retry_safety == "safe" or r.reused or r.status in {"completed", "failed"}
                for r in started_reports
            )
        ),
        result_schema=REPORT_SCHEMA,
    )


def _encode_reports(reports: list[dict[str, Any]]) -> str:
    import json

    return json.dumps({"commands": reports}, ensure_ascii=False)
