"""Authenticated loopback CLI with reusable credentials and command IDs."""

import argparse
import hashlib
import json
import time
import uuid
from contextlib import closing
from typing import Any
from urllib.parse import quote

import httpx
import portalocker

from agents_ide.config import Settings
from agents_ide.errors import AppError
from agents_ide.persistence.database import create_database
from agents_ide.security.auth import AuthService
from agents_ide.security.filesystem import atomic_write
from agents_ide.security.secrets import SecretStore


def add_arguments(subparsers: Any) -> None:
    for name in ("projects", "chats", "runs"):
        parser = subparsers.add_parser(name)
        parser.add_argument(
            "action",
            choices=(
                ["list", "create"]
                if name != "runs"
                else [
                    "start",
                    "status",
                    "events",
                    "artifacts",
                    "diagnostics",
                    "cleanup",
                    "pause",
                    "resume",
                    "stop",
                    "cancel",
                    "resolve",
                ]
            ),
        )
        parser.add_argument("--run-id")
        parser.add_argument("--project-id")
        parser.add_argument("--artifact-id")
        parser.add_argument("--command-id")
        from pathlib import Path

        parser.add_argument("--payload-json", type=Path)
        parser.add_argument("--after", type=int, default=0)
        parser.add_argument("--limit", type=int, default=200)
        parser.add_argument("--follow", action="store_true")
        parser.add_argument("--re-pair", action="store_true", help="Явно создать новую CLI-сессию")


def _body(args: argparse.Namespace) -> dict[str, Any]:
    if args.payload_json is None:
        return {}
    try:
        if args.payload_json.stat().st_size > 1024 * 1024:
            raise ValueError("Payload too large")
        body = json.loads(args.payload_json.read_text(encoding="utf-8"))
        if not isinstance(body, dict):
            raise ValueError("Object required")
        return body
    except (ValueError, OSError):
        raise AppError("payload_invalid", "Ожидается JSON-объект до 1 MiB в UTF-8", 422) from None


def _request(client: httpx.Client, method: str, path: str, **kwargs: Any) -> Any:
    try:
        response = client.request(method, path, **kwargs)
        data = response.json()
    except httpx.HTTPError:
        raise AppError("api_unavailable", "Локальный API недоступен", 503) from None
    except ValueError:
        raise AppError("api_invalid_response", "API вернул некорректный ответ", 502) from None
    if response.is_error or response.is_redirect:
        raise AppError(
            data.get("code", "api_error"),
            data.get("message", "Ошибка API"),
            response.status_code,
            data.get("details", {}),
        )
    return data


def connect(settings: Settings, *, re_pair: bool = False) -> httpx.Client:
    client = httpx.Client(
        base_url=settings.origin, timeout=10, trust_env=False, follow_redirects=False
    )
    cache = settings.data_dir / f"runtime/cli-session-{settings.port}.json"
    store = SecretStore(settings.data_dir / "secrets")
    try:
        with portalocker.Lock(str(settings.data_dir / "runtime/cli-auth.lock"), timeout=5):
            if re_pair:
                cache.unlink(missing_ok=True)
            credentials = None
            if cache.exists():
                try:
                    saved = json.loads(cache.read_text(encoding="utf-8"))
                    if saved["origin"] == settings.origin:
                        credentials = json.loads(store.get(saved["secret_ref"]))
                        if credentials.get("expires_at", 0) <= time.time():
                            credentials = None
                except (ValueError, KeyError, AppError):
                    credentials = None
            if credentials is None:
                if not (settings.data_dir / "runtime/pair-code").exists():
                    if not settings.database_path.exists():
                        raise AppError("pair_required", "Сначала запустите API", 409)
                    # Pairing remains a one-time, same-user flow. Reuse the CLI
                    # session on later invocations; never send credentials to an override URL.
                    engine = create_database(settings)
                    try:
                        AuthService(settings, engine).issue_code()
                    finally:
                        engine.dispose()
                code = (settings.data_dir / "runtime/pair-code").read_text(encoding="utf-8").strip()
                pair = _request(
                    client,
                    "POST",
                    "/api/auth/pair",
                    json={"code": code},
                    headers={"Origin": settings.origin},
                )
                credentials = {
                    "token": client.cookies.get("agents_ide_session"),
                    "csrf": pair["csrf_token"],
                    "expires_at": pair["expires_at"],
                }
                reference = store.put(json.dumps(credentials))
                atomic_write(
                    cache, json.dumps({"origin": settings.origin, "secret_ref": reference}).encode()
                )
            client.cookies.set("agents_ide_session", credentials["token"])
            client.headers.update({"Origin": settings.origin, "X-CSRF-Token": credentials["csrf"]})
        return client
    except BaseException:
        client.close()
        raise


def command_payload(settings: Settings, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Keep the exact request for a retry of the same command-id."""
    key = hashlib.sha256(f"{settings.origin}:{path}:{payload['command_id']}".encode()).hexdigest()
    file = settings.data_dir / f"runtime/cli-command-{key}.json"
    store = SecretStore(settings.data_dir / "secrets")
    with portalocker.Lock(str(settings.data_dir / "runtime/cli-commands.lock"), timeout=5):
        if file.exists():
            previous: dict[str, Any] = json.loads(
                store.get(json.loads(file.read_text(encoding="utf-8"))["secret_ref"])
            )
            if {k: v for k, v in previous.items() if k != "expected_state_version"} != {
                k: v for k, v in payload.items() if k != "expected_state_version"
            }:
                raise AppError(
                    "command_id_conflict", "command_id уже использован с другим содержимым", 409
                )
            return previous
        reference = store.put(json.dumps(payload))
        atomic_write(file, json.dumps({"secret_ref": reference}).encode())
        return payload


def execute(settings: Settings, args: argparse.Namespace) -> None:
    with closing(connect(settings, re_pair=args.re_pair)) as client:
        if args.command in {"projects", "chats"}:
            if args.command == "chats" and not args.project_id:
                raise AppError("argument_required", "Укажите --project-id", 422)
            path = (
                "/api/projects"
                if args.command == "projects"
                else f"/api/projects/{quote(args.project_id, safe='')}/chats"
            )
            result = (
                _request(client, "GET", path)
                if args.action == "list"
                else _request(client, "POST", path, json=_body(args))
            )
        elif args.action == "start":
            body = _body(args)
            body.setdefault("idempotency_key", args.command_id or uuid.uuid4().hex)
            result = _request(client, "POST", "/api/runs", json=body)
        elif args.action == "status" and not args.run_id:
            result = _request(client, "GET", "/api/runs")
        else:
            if not args.run_id:
                raise AppError("argument_required", "Укажите --run-id", 422)
            path = f"/api/runs/{quote(args.run_id, safe='')}"
            if args.action == "status":
                result = _request(client, "GET", path)
            elif args.action == "artifacts":
                suffix = f"/{quote(args.artifact_id, safe='')}" if args.artifact_id else ""
                result = _request(client, "GET", path + "/artifacts" + suffix)
            elif args.action == "diagnostics":
                result = _request(client, "GET", path + "/diagnostics")
            elif args.action == "events":
                after = args.after
                while True:
                    result = _request(
                        client,
                        "GET",
                        path + "/events",
                        params={"after": after, "limit": args.limit},
                    )
                    print(json.dumps(result, ensure_ascii=False))
                    if result["reset_required"]:
                        return
                    after = result["last_sequence"]
                    if result["has_more"]:
                        continue
                    if not args.follow or _request(client, "GET", path)["state"] in {
                        "completed",
                        "failed",
                        "cancelled",
                    }:
                        return
                    time.sleep(0.25)
            else:
                row = _request(client, "GET", path)
                command_id = args.command_id or uuid.uuid4().hex
                payload = {"command_id": command_id, "expected_state_version": row["state_version"]}
                if args.action == "cleanup":
                    result = _request(
                        client,
                        "POST",
                        path + "/reservations/cleanup",
                        json=command_payload(settings, path, payload),
                    )
                else:
                    payload.update(command_type=args.action, payload=_body(args))
                    result = _request(
                        client,
                        "POST",
                        path + "/commands",
                        json=command_payload(settings, path, payload),
                    )
        print(json.dumps(result, ensure_ascii=False))
