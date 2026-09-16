import argparse
import json
import logging
import os
import sqlite3
import sys
import threading
from pathlib import Path

import portalocker
import uvicorn
from sqlalchemy.exc import SQLAlchemyError

from agents_ide import launcher
from agents_ide.api.app import create_app
from agents_ide.config import Settings
from agents_ide.errors import AppError
from agents_ide.logging import configure_logging
from agents_ide.persistence.database import create_database, migrate
from agents_ide.security.auth import AuthService
from agents_ide.security.filesystem import prepare_data_dir
from agents_ide.worker.main import run_worker


def run_api(settings: Settings) -> None:
    with portalocker.Lock(str(settings.data_dir / "runtime/api.lock"), timeout=0):
        from agents_ide.operations.maintenance import ensure_available

        ensure_available(settings)
        launcher.check_port(settings)
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(settings),
                host=settings.host,
                port=settings.port,
                access_log=False,
                log_config=None,
            )
        )
        finished = threading.Event()

        def monitor() -> None:
            while not finished.wait(0.25):
                if launcher.stop_requested(settings, os.environ.get("AGENTS_IDE_LAUNCH_ID")):
                    server.should_exit = True
                    return

        thread = threading.Thread(target=monitor, daemon=True)
        thread.start()
        try:
            server.run()
        finally:
            finished.set()
            thread.join(timeout=1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Agents IDE local services")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--port", type=int)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("api", "worker", "migrate", "start", "status", "stop", "_launcher"):
        subparsers.add_parser(command)
    auth = subparsers.add_parser("auth")
    auth.add_argument("action", choices=["pair-code"])
    auth.add_argument("--rotate", action="store_true")
    from agents_ide.diagnostic_cli import add_arguments

    add_arguments(subparsers)
    from agents_ide.operations.cli import add_arguments as add_operations

    add_operations(subparsers)
    args = parser.parse_args()
    try:
        overrides = {}
        if args.data_dir is not None:
            overrides["data_dir"] = args.data_dir
        if args.port is not None:
            overrides["port"] = args.port
        settings = Settings(**overrides)
        prepare_data_dir(settings.data_dir)
        configure_logging(settings.data_dir / "logs", args.command.lstrip("_"), settings.log_level)
        match args.command:
            case "api":
                run_api(settings)
            case "worker":
                run_worker(settings)
            case "migrate":
                from agents_ide.operations.maintenance import offline

                with offline(settings, "migrate"):
                    migrate(settings, lock_held=True)
                print("Database migrated.")
            case "auth":
                from agents_ide.operations.maintenance import ensure_available

                with portalocker.Lock(
                    str(settings.data_dir / "runtime/operations.lock"), timeout=0
                ):
                    ensure_available(settings)
                    migrate(settings)
                    engine = create_database(settings)
                    try:
                        print(AuthService(settings, engine).issue_code(rotate=args.rotate))
                    finally:
                        engine.dispose()
            case "start":
                print(json.dumps(launcher.start(settings)))
            case "status":
                print(json.dumps(launcher.status(settings)))
            case "stop":
                print(json.dumps(launcher.stop(settings)))
            case "_launcher":
                launcher.run_launcher(settings)
            case "runs" | "projects" | "chats":
                from agents_ide.diagnostic_cli import execute

                execute(settings, args)
            case _:
                from agents_ide.operations.cli import execute as execute_operation

                execute_operation(settings, args)
    except AppError as error:
        logging.error(error.code)
        print(f"{error.code}: {error.message}", file=sys.stderr)
        raise SystemExit(1) from None
    except portalocker.LockException:
        print("service_busy: another instance holds the service lock", file=sys.stderr)
        raise SystemExit(1) from None
    except OSError:
        print("storage_unavailable: проверьте диск, права доступа и diagnostics", file=sys.stderr)
        raise SystemExit(1) from None
    except (SQLAlchemyError, sqlite3.Error):
        print("database_unavailable: проверьте БД, диск и backup", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
