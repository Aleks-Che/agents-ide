import logging
import sqlite3
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import portalocker
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import URL, Engine, create_engine, event
from sqlalchemy.pool import QueuePool

from agents_ide.config import Settings

MIGRATIONS_DIRECTORY = Path(__file__).parent / "migrations"


def _schema_revision() -> str:
    revision = ScriptDirectory(str(MIGRATIONS_DIRECTORY)).get_current_head()
    if revision is None:
        raise RuntimeError("Database migrations have no head revision")
    return revision


# Use the same migration head as upgrade(); a second, manually maintained
# version made healthy workers stop immediately after new migrations shipped.
SCHEMA_REVISION = _schema_revision()


def create_database(settings: Settings) -> Engine:
    engine = create_engine(
        URL.create("sqlite", database=str(settings.database_path)),
        # Short SQLite waits are polling intervals, never an execution deadline.
        connect_args={"check_same_thread": False, "timeout": 0.1},
        # Keep page caches and WAL handles across short polls. NullPool reopened
        # the file for every query and checkpointed when the last connection closed.
        poolclass=QueuePool,
        pool_size=5,
        # Keep only five idle connections, without capping concurrent dispatches
        # or making nested sessions wait for another session's connection.
        max_overflow=-1,
    )

    @event.listens_for(engine, "connect")
    def configure(connection: sqlite3.Connection, _: object) -> None:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=100")
        connection.execute("PRAGMA synchronous=FULL")

    @event.listens_for(engine, "do_execute")
    def wait_for_writer(cursor: Any, statement: str, parameters: Any, context: Any) -> bool:
        assert isinstance(cursor, sqlite3.Cursor)
        while True:
            cancel = context.execution_options.get("cancel_wait")
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Database wait cancelled")
            try:
                cursor.execute(statement, parameters)
                return True
            except sqlite3.OperationalError as exc:
                # A stale WAL read snapshot cannot become writable by waiting;
                # retrying it would deadlock. Likewise, real I/O errors must surface.
                if getattr(exc, "sqlite_errorcode", None) not in {
                    sqlite3.SQLITE_BUSY,
                    sqlite3.SQLITE_BUSY_RECOVERY,
                }:
                    raise
                time.sleep(0.05)

    @event.listens_for(engine, "engine_connect")
    def settings_context(connection: object) -> None:
        from sqlalchemy import Connection

        assert isinstance(connection, Connection)
        connection.info["storage_settings"] = settings

    return engine


def migrate(settings: Settings, *, lock_held: bool = False) -> None:
    # Launcher/API/worker can start simultaneously; migration has one owner.
    guard = (
        nullcontext()
        if lock_held
        else portalocker.Lock(
            str(settings.data_dir / "runtime/migrate.lock"), flags=portalocker.LOCK_EX
        )
    )
    with guard:
        engine = create_database(settings)
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA journal_mode=WAL")
                connection.commit()
                config = Config()
                config.set_main_option("script_location", str(MIGRATIONS_DIRECTORY))
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
        finally:
            engine.dispose()


def check_database(engine: Engine) -> bool:
    with engine.connect() as connection:
        revisions = list(
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalars()
        )
        ready = revisions == [SCHEMA_REVISION]
        if not ready:
            logging.getLogger(__name__).error(
                "database.schema_mismatch",
                extra={"expected_schema": SCHEMA_REVISION, "actual_schema": revisions},
            )
        return ready
