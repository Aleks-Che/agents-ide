import logging
import sqlite3
from contextlib import nullcontext
from pathlib import Path

import portalocker
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import URL, Engine, create_engine, event

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
        connect_args={"check_same_thread": False, "timeout": 5},
    )

    @event.listens_for(engine, "connect")
    def configure(connection: sqlite3.Connection, _: object) -> None:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=FULL")

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
        else portalocker.Lock(str(settings.data_dir / "runtime/migrate.lock"), timeout=30)
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
