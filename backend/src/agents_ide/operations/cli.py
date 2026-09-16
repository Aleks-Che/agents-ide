import argparse
import json
from pathlib import Path
from typing import Any

import portalocker
from sqlalchemy.orm import Session, sessionmaker

from agents_ide.config import Settings
from agents_ide.operations import backup as backups
from agents_ide.operations.diagnostics import diagnostics
from agents_ide.operations.maintenance import clear_interrupted, ensure_available, offline
from agents_ide.operations.storage import collect_garbage, set_pin
from agents_ide.persistence.database import create_database


def add_arguments(subparsers: Any) -> None:
    subparsers.add_parser("diagnostics")
    backup = subparsers.add_parser("backup")
    backup.add_argument("destination", type=Path)
    backup.add_argument("--include-secrets", action="store_true")
    restore = subparsers.add_parser("restore")
    restore.add_argument("source", type=Path)
    verify = subparsers.add_parser("verify-backup")
    verify.add_argument("source", type=Path)
    update = subparsers.add_parser("update")
    update.add_argument("--backup-dir", type=Path)
    update.add_argument("--check", action="store_true")
    subparsers.add_parser("gc")
    subparsers.add_parser("compact")
    subparsers.add_parser("maintenance-clear")
    pin = subparsers.add_parser("pin")
    pin.add_argument("run_id")
    pin.add_argument("--remove", action="store_true")


def execute(settings: Settings, args: argparse.Namespace) -> None:
    result: Any
    match args.command:
        case "diagnostics":
            result = diagnostics(settings)
        case "backup":
            result = backups.backup(
                settings, args.destination, include_secrets=args.include_secrets
            )
        case "restore":
            result = backups.restore(settings, args.source)
        case "verify-backup":
            manifest = backups.verify_backup(args.source.absolute())
            result = {"valid": True, "database_revision": manifest["database_revision"]}
        case "update":
            if args.check:
                result = backups.compatibility(settings)
            elif args.backup_dir:
                result = backups.update(settings, args.backup_dir)
            else:
                from agents_ide.errors import AppError

                raise AppError("backup_required", "Укажите --backup-dir или --check", 422)
        case "compact":
            result = backups.compact(settings)
        case "maintenance-clear":
            clear_interrupted(settings)
            result = {"maintenance": "cleared"}
        case "gc":
            with offline(settings, "gc"):
                engine = create_database(settings)
                try:
                    result = collect_garbage(sessionmaker(engine, expire_on_commit=False))
                finally:
                    engine.dispose()
        case "pin":
            with portalocker.Lock(str(settings.data_dir / "runtime/operations.lock"), timeout=0):
                ensure_available(settings)
                engine = create_database(settings)
                try:
                    with Session(engine) as session:
                        result = set_pin(session, args.run_id, not args.remove)
                finally:
                    engine.dispose()
        case _:
            raise ValueError("unknown maintenance command")
    print(json.dumps(result, ensure_ascii=False))
